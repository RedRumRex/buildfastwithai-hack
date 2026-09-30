# 🎯 LeadLens: AI Decision Engine for Sales Data

**AI Build Challenge 2026 · Brief 04 (Data / Business Intelligence)**

LeadLens takes a sales team's messy CRM data (leads, deals, activity, call notes) and answers one question: **"Who should we contact this week, and why?"**

- **Cleans the mess.** It merges duplicate records ("Acme Corp" vs "ACME Inc.", "P. Sharma" vs "Priya Sharma"), flags stale and unreachable leads, and shows a data health report.
- **Answers questions in plain English.** Number questions go through text-to-SQL, and the SQL is shown. Questions about what customers said go through retrieval over call notes, and the note IDs are cited.
- **Decides who to contact first.** A transparent scoring rubric ranks every lead, and each point links to the exact rows behind it.
- **Keeps a human in charge.** The AI only suggests. A reviewer approves, edits or rejects every action, and each decision goes to an audit log along with its evidence IDs.

> **Design principle:** the math decides, the AI explains, and the human approves.
> The LLM never computes a score or a number. Scores come from explicit rules, and numbers come from executed SQL.

---

## Quick start (about 2 minutes)

```bash
cd leadlens
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python data/generate_data.py          # optional: sample data is already included
streamlit run app.py
```

Open http://localhost:8501.

### Turn on the AI (optional but recommended)
The app works fully **without** an API key, using built-in query templates and template explanations, so your demo can't break. To use an LLM for free-form questions, explanations and email drafts:

```bash
cp .env.example .env     # then paste a key
```
Any OpenAI-compatible provider works: **Groq** and **Gemini** have free tiers, and OpenAI, OpenRouter and Ollama work too. See `.env.example`.

---

## Architecture

```
 leads.csv ─┐
 deals.csv ─┤   core/clean.py            core/scoring.py              app.py (Streamlit)
 activity ──┼─► normalise → dedupe ─┬─► transparent rubric ──► ranked queue ─► Approve / Reject ─► audit_log.csv
 notes.csv ─┘   (email / fuzzy name)│   + evidence row ids     + "Why?" panel      + email draft
                stale flags, health │
                                    └─► DuckDB (core/qa.py) ◄── "Ask your data"
                                          ├─ text-to-SQL  (numbers, shows SQL, read-only guard)
                                          └─ notes retrieval / RAG (cites note ids)
```

| File | What it does |
|---|---|
| `data/generate_data.py` | Synthetic B2B SaaS CRM with **injected** duplicates (incl. renamed companies and email typos), missing, bounced and invalid emails, missing phones, stale records and conflicting values. Deterministic; writes the dedupe ground truth `data/truth_duplicates.csv` |
| `core/ingest.py` | Upload validation: maps header variants (`Email`, `E-mail`, `Company Name`…), clear errors for missing columns / unreadable files / non-date columns, warnings for filled defaults, duplicate IDs and orphan rows |
| `core/clean.py` | Entity resolution (same email · same company + fuzzy or initial name · same phone + name · mistyped email + name · name + same email name + same firmographics), a "possible duplicate: needs review" list for borderline pairs, field merging, re-linking child records, stale flags, before/after health report with field-level issues, merge log |
| `core/scoring.py` | Decision agent: 7-component rubric (deal size, 30-day engagement, close urgency, momentum, ICP fit, call-note signals, data quality), plus the suggested next action |
| `core/qa.py` | Analytics agent: question router, LLM text-to-SQL with a retry-on-error loop, read-only SQL guard, rule-based fallback queries, and TF-IDF retrieval over call notes |
| `core/actions.py` | Grounded explanations, email drafts, audit trail (approve / reject / edit / undo) |
| `core/filters.py` | Plain-English queue filters: LLM parser with validated output, rule-based fallback |
| `core/llm.py` | OpenAI-compatible client wrapper |
| `app.py` | UI with 5 tabs: Data health · Action queue · Ask your data · Audit log · How scoring works |

### Scoring rubric (from `core/scoring.py`)
| Component | Rule |
|---|---|
| Deal size | up to 30 pts, largest open deal, log-scaled |
| Engagement (30 days) | up to 30 pts: demo request 6, meeting 5, pricing visit 4, call 3, click 2, open 1 |
| Close urgency | 20 pts if closing within 30 days, 10 pts within 60 days |
| Deal momentum | 5 pts if in Proposal Sent or Negotiation |
| Ideal customer fit | target industry 5 + company size ≥ 200 gives 5 |
| Call-note signals | +8 for a buying signal, −10 for an objection (last 90 days) |
| Data quality | −15 if stale or unreachable, −5 if no contact in 60+ days |

**Editable weights.** Every number above is a default. In the **⚙️ How scoring works** tab a manager can move the sliders
(or pick a preset like *Close this quarter*) and the action queue, the "Why?" panels and the `lead_scores` table used by
*Ask your data* re-rank instantly. Weights only change how much each fact counts; the facts and their source row IDs never change.
Internally, `extract_signals()` reads the data once (~1 s) and `apply_weights()` turns signals into points (~15 ms).

### Dedupe evaluation (precision / recall / false merges)
```bash
python data/generate_data.py      # also writes data/truth_duplicates.csv
python data/plant_hot_leads.py    # re-plant the hot leads after regenerating
python eval/eval_dedupe.py --seeds 10   # writes eval/results_dedupe.md (+ robustness on 10 fresh datasets)
```
Scored on pairs of raw records against the injected duplicates. Pairs that are only *possibly* the same person are not merged
but listed for review; the report shows recall with and without that list. Target: recall ≥ 98%, 0 false merges.

### Ranking evaluation (precision@20)
```bash
python data/plant_hot_leads.py    # plants 30 known hot leads + 16 decoys, writes data/truth_hot_leads.csv (idempotent)
python eval/eval_ranking.py       # prints the report and writes eval/results_ranking.md
```
"Truly hot" = the planted hot leads plus any organic lead that passes the same sales-manager checklist (reachable, contacted
in 30 days, open deal ≥ $20k in Demo/Proposal/Negotiation closing within 60 days, a demo/meeting/pricing visit, no recent objection).
The report also shows the strict number (planted leads only) and where each decoy type ranks.

---

## Deploy to Streamlit Community Cloud (free public link)

1. Push `main` to GitHub (the repo must contain `app.py`, `requirements.txt` and the `data/` CSVs – it does).
2. Go to **share.streamlit.io**, sign in with GitHub and allow access to the repo.
3. **Create app → deploy from GitHub**: repository `RedRumRex/buildfastwithai-hack`, branch `main`, main file `app.py`.
4. **Advanced settings**
   - Python version: **3.12** (tested; 3.10–3.12 all give identical scores).
   - Secrets: paste the three lines from `.streamlit/secrets.toml.example` with your real key. Leave empty to run on the
     built-in rules only – the app still works end to end.
5. **Deploy.** The first build takes a few minutes; later pushes to `main` redeploy automatically.

Things to know about the hosted app:
- Every visitor shares one server. Database access is serialised with a lock (8 simultaneous visitors tested:
  no errors, each sees their own scoring weights).
- The audit log lives on the server's disk: all visitors see the same log, and it resets when the app restarts.
  Use **Export full log (CSV)** if you want to keep it.
- Free apps go to sleep when nobody uses them. **Open the link a few minutes before the demo** to wake it up.

## Before every demo or merge

```bash
python eval/demo_check.py      # clicks through the whole demo headlessly – must end with "DEMO READY"
python eval/eval_ranking.py    # precision@20 must be >= 80%
```
Then delete `data/audit_log.csv` locally so the Audit log tab starts empty.

## 3-minute demo script

1. **Data health tab.** "We loaded 2,246 raw CRM records. LeadLens merged 196 duplicates with zero false merges, like 'Hayley Gibson @ dube' and 'Hayley Gibson @ Dube Group', or 'M. Briggs @ Davis Corp' and 'Michael Briggs @ Badal Corp', who share a phone number after a company rename. 12 borderline pairs are listed for a human to review instead of being merged, and about 21% of leads are stale or unreachable." Point at the before/after table, the merge log and its evidence column.
2. **Action queue tab.** "Here's who to contact this week." Open #1 and read the *Why this lead* line.
3. Flip **🔍 Why?** to show the actual deal, activity and note rows behind every point. "Nothing here is made up."
4. Type in the refine box: `skip anyone contacted in the last 2 weeks, FinTech only, late-stage deals over $20k`. The queue re-ranks, and the parsed filters show as chips (with whether the AI or the rule-based parser understood it).
5. **Approve** one lead, optionally editing the action first. An outreach email is drafted and nothing is sent automatically. Edit the draft, **Save edits**, and download it as an `.eml` that opens as an unsent draft in Mail/Outlook. **Reject** another, or tick several leads and use **bulk approve / reject**.
6. **Ask your data tab.** Click "Which deals over $50k are stuck?" to show the answer, the SQL behind it and the rows. Then click "Who complained about pricing?" to show the answer with note-ID citations.
7. **Audit log tab.** "Every approve, reject, email edit and undo is recorded with who made it, when, the evidence IDs and the weights used." Filter by decision or lead and export the filtered view as CSV.
8. Closer: **"Every number is traceable, and no action happens without a human."**

## Using your own data
Choose **Upload my CSVs** in the sidebar and give it four files with the same columns as the files in `data/`. Your records are cleaned, scored and queryable immediately.
