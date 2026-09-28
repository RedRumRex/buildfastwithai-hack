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
| `data/generate_data.py` | Synthetic B2B SaaS CRM with **injected** duplicates, missing and bounced emails, stale records and conflicting values |
| `core/clean.py` | Entity resolution (same email, or same company + fuzzy or initial name match), field merging, re-linking child records, stale flags, health report, merge log |
| `core/scoring.py` | Decision agent: 7-component rubric (deal size, 30-day engagement, close urgency, momentum, ICP fit, call-note signals, data quality), plus the suggested next action |
| `core/qa.py` | Analytics agent: question router, LLM text-to-SQL with a retry-on-error loop, read-only SQL guard, rule-based fallback queries, and TF-IDF retrieval over call notes |
| `core/actions.py` | Grounded explanations, email drafts, audit trail |
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

---

## 3-minute demo script

1. **Data health tab.** "We loaded 2,140 raw CRM records. LeadLens found and merged 137 duplicates, like 'M. Brown @ Bal Ltd' and 'Marc Brown @ Bal Corp', and flagged about 21% as stale." Point at the merge log and its evidence column.
2. **Action queue tab.** "Here's who to contact this week." Open #1 and read the *Why this lead* line.
3. Flip **🔍 Why?** to show the actual deal, activity and note rows behind every point. "Nothing here is made up."
4. Type in the refine box: `skip anyone contacted in the last 2 weeks, FinTech only, over $20k`. The queue re-ranks, and the parsed filters show as chips.
5. **Approve** one lead, optionally editing the action first. An outreach email is drafted and nothing is sent automatically. **Reject** another.
6. **Ask your data tab.** Click "Which deals over $50k are stuck?" to show the answer, the SQL behind it and the rows. Then click "Who complained about pricing?" to show the answer with note-ID citations.
7. **Audit log tab.** "Every decision is recorded with who made it, when, and the evidence IDs."
8. Closer: **"Every number is traceable, and no action happens without a human."**

## Using your own data
Choose **Upload my CSVs** in the sidebar and give it four files with the same columns as the files in `data/`. Your records are cleaned, scored and queryable immediately.

## Ideas if you have extra time
- Swap TF-IDF for embeddings (Chroma or pgvector) in `DataStore.search_notes`
- Connect a real CRM (HubSpot or Salesforce API) instead of CSVs
- "Send" approved emails through Gmail or SMTP, still only after approval
- Learn the scoring weights from won/lost history, and show them next to the rules
