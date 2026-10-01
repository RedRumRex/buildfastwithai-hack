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
