#  LeadLens — AI Decision Engine for Sales Data

** AI Build Challenge 2026 · PS-04: AI Decision Engine for Business Data **
*"Build an AI system that analyses business data, generates traceable insights, and recommends decisions or actions grounded in the underlying data."*

LeadLens turns a sales team's messy CRM export into one clear answer:

> ## Who should we contact this week — and why?

It cleans the data, ranks every lead with a transparent scoring rubric, explains each recommendation with links to the exact rows behind it, answers plain-English questions about the pipeline, and keeps a human in charge of every action.

**▶ Live app:** [leadlens.streamlit.app](https://leadlens.streamlit.app)  ·  ** How to use, upload and deploy:** [INSTRUCTIONS.md](INSTRUCTIONS.md)

---

## Contents
1. [The problem](#1-the-problem)
2. [What LeadLens does](#2-what-leadlens-does)
3. [Design principle](#3-design-principle)
4. [Architecture](#4-architecture)
5. [Tech stack](#5-tech-stack)
6. [The app, tab by tab](#6-the-app-tab-by-tab)
7. [How scoring works](#7-how-scoring-works)
8. [Asking questions: text-to-SQL and call-note search](#8-asking-questions-text-to-sql-and-call-note-search)
9. [Trust, safety and fallbacks](#9-trust-safety-and-fallbacks)
10. [Results](#10-results)
11. [Testing](#11-testing)
12. [Repository layout](#12-repository-layout)
13. [Quick start](#13-quick-start)
14. [Team](#14-team)

---

## 1. The problem

B2B sales teams sit on thousands of CRM records but still decide who to call by gut feel:

- **The data is messy.** The same person appears three times ("P. Sharma @ ACME Corp", "Priya Sharma @ Acme Inc.", "PRIYA SHARMA"), emails bounce, contact dates go stale, and activity is split across duplicates.
- **The signals are scattered.** Deal size lives in one table, email opens and demo requests in another, and "budget approved" is buried in a call note.
- **AI answers can't be trusted blindly.** A chatbot that invents a number or a customer quote is worse than no answer at all.
- **Nobody can audit the decision.** Why was this lead prioritised? Who approved the email? Based on what?

## 2. What LeadLens does

| Step | What happens |
|---|---|
| **1. Ingest** | Loads 4 CSVs (leads, deals, activity, call notes) — the built-in sample CRM or your own upload. Header variants like `E-mail` or `Company Name` are recognised. |
| **2. Clean** | Merges duplicate leads (fuzzy names, initials, company suffixes, phone numbers, email typos), re-links their deals / activity / notes, flags stale and unreachable contacts, and produces a before → after data-health report. |
| **3. Score** | Ranks every lead with a 7-part rubric (deal size, engagement, close urgency, momentum, customer fit, call-note signals, data quality). **Every point stores the IDs of the rows that earned it.** |
| **4. Explain** | Writes a one-to-two sentence reason for each top lead, using only the scored facts, with citations that are checked against the data. |
| **5. Recommend** | Suggests the next best action (e.g. *"Call today to push the deal over the line before close date"*). |
| **6. Ask** | Answers plain-English questions: numbers via text-to-SQL (the SQL is shown), "what did customers say" via search over call notes (note IDs are cited). |
| **7. Approve** | A person approves, edits or rejects every action — one by one or in bulk. Approval drafts an email the rep can edit and download. Nothing is sent automatically. |
| **8. Audit** | Every approve, reject, edit and undo is logged with who, when, the evidence IDs and the scoring weights used. |

## 3. Design principle

> **The math decides, the AI explains, the human approves.**

- **Scores come from explicit rules**, never from the language model.
- **Numbers come from executed SQL**, never from the language model. An AI summary that mentions a number not present in the query result is hidden and the raw result is shown instead.
- **Every claim is traceable** to row IDs (`L…` lead, `D…` deal, `A…` activity, `N…` note) that you can open in the app.
- **Every AI feature has a rule-based fallback**, so the app works fully without an API key and never hangs on a slow one.

## 4. Architecture

```
                 ┌──────────────────────────── DATA LAYER ────────────────────────────┐
  leads.csv  ─┐  │ core/ingest.py          core/clean.py                              │
  deals.csv  ─┤  │ header mapping,  ──►    normalise → dedupe (5 rules + review band) │
  activity   ─┼─►│ type checks,            → re-link child rows → stale flags         │
  notes.csv  ─┘  │ clear errors            → health report + merge log                │
                 └──────────────────────────────────┬─────────────────────────────────┘
                                                    │ CleanResult (clean tables)
                 ┌──────────────────────────────────▼─────────────────────────────────┐
                 │   DuckDB (in-memory, read-only for queries)  +  call-notes index    │
                 └────────────┬──────────────────────────────────────────┬────────────┘
                              │                                          │
  ┌────────── AI LAYER ───────▼───────────────┐   ┌────── DECISION + APP LAYER ───────▼───────┐
  │ core/qa.py      router                    │   │ core/scoring.py                           │
  │   ├─ text-to-SQL (few-shot, retry loop,   │   │   extract_signals() → facts + row IDs     │
  │   │   read-only guard, number check)      │   │   apply_weights()   → points (~15 ms)     │
  │   └─ call-note search (TF-IDF or          │   │   suggest_action()                        │
  │      embeddings), cites note IDs          │   │                                           │
  │ core/rag.py, core/citations.py            │   │ core/filters.py  plain-English filters    │
  │ core/llm.py  timeouts, retries,           │◄──┤ app.py (Streamlit, 5 tabs)                │
  │              circuit breaker              │   │   Approve / Edit / Reject / Undo          │
  │ core/actions.py explanations, emails ─────┼──►│   ──► audit_log.csv                       │
  └───────────────────────────────────────────┘   └───────────────────────────────────────────┘
```

**Data flow in one line:** CSVs → validate → clean & dedupe → DuckDB → (a) rule-based scoring → ranked queue → human approval → audit log; (b) questions → text-to-SQL or note search → cited answer.

## 5. Tech stack

| Area | Choice | Why |
|---|---|---|
| Language | **Python 3.10 – 3.12** | One language for data, AI and UI |
| UI | **Streamlit** | Fast, interactive, free hosting |
| Database | **DuckDB** (in-memory) | Real SQL over dataframes, no server to run |
| Data cleaning | **pandas**, **RapidFuzz** | Fuzzy name / company / email matching |
| Notes search | **scikit-learn TF-IDF** (default); sentence-transformers + Chroma embeddings if installed | Works everywhere; embeddings are an optional upgrade |
| LLM | Any **OpenAI-compatible API** — deployed with **Groq `openai/gpt-oss-120b`** | Fast, free tier; switch provider via secrets only |
| Sample data | **Faker** | Realistic synthetic CRM with planted errors and ground truth |
| Config | **python-dotenv** / Streamlit Secrets | Keys never in the code |
| Hosting | **Streamlit Community Cloud** | Free public link |

## 6. The app, tab by tab

**Sidebar** — choose *Sample CRM* or *Upload my CSVs*, set the reviewer name used in the audit log, and see whether the LLM is connected.

###  Data health
Proves the data was cleaned and shows what changed.
- Headline numbers: raw records, duplicates merged, possible duplicates, clean leads, stale/unreachable leads, missing/bounced/invalid emails.
- **Before → after cleaning** table and **field-level issues** (missing emails, invalid formats, ALL-CAPS names, stale dates…) with example IDs.
- **Merge log** — every duplicate that was merged, the rule that matched it and the evidence (e.g. `'R. Brown' @ 'BROWN PVT LTD' ~ 'Roger Brown' @ 'Brown Pvt Ltd'`).
- **Why records are stale** chart and a **possible duplicates — needs review** list for borderline pairs a person should decide on.

###  Action queue
The heart of the app: who to contact, why, and what to do.
- **Plain-English filter** — e.g. *"skip anyone contacted in the last 2 weeks, FinTech only, late-stage deals over $20k, top 15"*. Parsed by the LLM (validated) or by built-in rules; the understood filters appear as chips. Manual filters: top N, skip recently contacted, industries, deal stage, minimum deal, hide stale.
- **One card per lead** with the score, an AI explanation, the suggested action and a points breakdown.
- ** Why?** — shows the actual deal, activity and note rows behind every point.
- **Approve / Reject** (with an editable action and a note), or **bulk approve / reject** several leads at once.
- Approving drafts an **email** you can edit, save (logged) and download as `.eml` (opens as an unsent draft in Mail/Outlook), or download all drafts as a zip. **Undo** any decision.

###  Ask your data
Plain-English questions about the pipeline, with six one-click examples. Number questions show the **SQL** and the **result rows**; "what did customers say" questions show the **source call notes** with their IDs. Each answer says which engine produced it (text-to-SQL, notes search, or built-in fallback).

###  Audit log
Every approve, reject, edit and undo — append-only. Shows who, when, the lead, score, action, note, email subject and body, the **evidence IDs** and the **scoring weights** in force. Filter by decision, reviewer, text, date range or bulk-only; export the filtered view or the full log as CSV.

###  How scoring works
The scoring rubric, fully editable. Sliders for every component, engagement event weights, four presets (*Balanced*, *Close this quarter*, *Engagement first*, *Big deals only*) and reset. Every tab re-ranks instantly. Shows the rubric with the current weights, how the top 15 moved versus the defaults, and — on the sample data — the live **precision@20** of the ranking.

## 7. How scoring works

Each lead gets points from 7 components (default weights shown). The score is the sum.

| Component | Default points | Rule |
|---|---|---|
| **Deal size** | up to 30 | Largest open deal, log-scaled: $1k ≈ 0, $10k = 15, $100k+ = 30 |
| **Engagement (last 30 days)** | up to 30 | Weighted events — demo request 6, meeting 5, pricing-page visit 4, call 3, email click 2, email open 1. Full points at 20 weighted events |
| **Close urgency** | 20 / 10 | An open deal closes within 30 days → 20; within 31–60 days → 10 |
| **Deal momentum** | 5 | A deal is in *Proposal Sent* or *Negotiation* |
| **Ideal customer fit** | 5 + 5 | Target industry (FinTech, Healthcare, E-commerce, Logistics) +5; company size ≥ 200 +5 |
| **Call-note signals** | +8 / −10 | Most recent signal note in 90 days: buying signal ("budget approved", "pilot", "proposal"…) +8; objection ("went with a competitor", "pricing concerns"…) −10 |
| **Data quality / recency** | −15 / −5 | Stale or unreachable (no contact in 180+ days, missing/bounced/invalid email) −15; no contact in 60+ days or no contact date −5 |

**Worked example.** A lead with a $50k deal in *Proposal Sent* closing in 20 days, 2 meetings + 1 pricing visit + 3 email opens this month, FinTech with 500 employees, and a "budget approved" note:
25.5 (deal) + 25.5 (engagement: 17/20 × 30) + 20 (urgency) + 5 (momentum) + 10 (fit) + 8 (note) = **94**.
A $150k deal whose contact's email bounced and who hasn't been reached in 250 days scores about **40** — a big deal you can't reach is not a lead to call this week.

**Points = how strongly the fact is true × weight.** The engine first extracts the facts once (`extract_signals()`, ~1–2 s), then multiplies by the weights (`apply_weights()`, ~15 ms). That's why the sliders re-rank instantly, and why changing a weight never changes the evidence.

**Suggested next action** (rule-based, from the facts, not the points): find a working contact → re-engage after an objection → call today before the close date → negotiation call → book a demo / pricing walkthrough → follow-up with a case study → intro email.

## 8. Asking questions: text-to-SQL and call-note search

1. **Router** — counting / number questions go to SQL; "said / mentioned / complained / concern / competitor / feedback…" questions go to call-note search.
2. **Text-to-SQL** — the LLM sees the schema, rules and 5 few-shot examples (none from the test set) and returns one DuckDB query. It runs through the read-only guard; if it fails, the error is fed back for up to 3 tries; if it still fails, a built-in query answers instead.
3. **Summary** — the LLM summarises the result rows. Record IDs it cites are checked against the data, and a summary that mentions a number not in the result is hidden.
4. **Call notes** — the most relevant notes are retrieved (TF-IDF by default, embeddings if installed) and the LLM answers *only* from them, citing note IDs. Invented IDs are removed.

## 9. Trust, safety and fallbacks

| Risk | How LeadLens handles it |
|---|---|
| LLM invents a score | Scores are pure rules; the LLM never sees or sets them |
| LLM invents a number | Numbers come from executed SQL; summaries with numbers not in the result are hidden |
| LLM invents a record ID | Citation checker removes IDs that don't exist (or, for explanations, aren't part of that lead's evidence) |
| Internal IDs leak to customers | Email drafts are stripped of all record IDs |
| SQL modifies data | Only a single `SELECT`/`WITH` is accepted, and every query is wrapped in `SELECT * FROM (…)` — two independent guards |
| SQL reads server files (e.g. the API key) | DuckDB file and network access is switched off and locked after loading the data |
| LLM is slow, rate-limited or down | 20 s timeout, retries with back-off, then a circuit breaker switches to rules instantly for 60 s |
| No API key at all | Every feature falls back to rules / templates; the whole demo still runs |
| Many visitors at once | Database access is serialised; each visitor keeps their own scoring weights |
| Bad upload | Clear errors per file; the app falls back to the sample data instead of crashing |

## 10. Results

All numbers are reproducible with the scripts in `eval/`.

| Metric | Result | Target |
|---|---|---|
| Duplicate detection — recall | **98.0%** (196 / 200), 100% incl. the review list | ≥ 98% |
| Duplicate detection — false merges | **0** (precision 100%) | 0 |
| Ranking — precision@20 (default weights) | **95%** (strict, planted leads only: 75%) · **0** decoys in top 20 | ≥ 80% |
| Text-to-SQL — execution accuracy (50 questions, `gpt-oss-120b`) | **94%** (98% of LLM-answered) | ≥ 85% |
| Live re-check (35 questions, Oct 2026) | **100%** correct, 0 fallbacks | |
| Citations shown to users | **100%** valid (raw model output 99.6–100%) | 100% |
| Full demo, headless | **13 / 13** steps, zero errors | zero errors |
| Scale | 5,500-lead CRM cleaned + scored in ≈ 13 s | |

**How precision@20 is measured.** `data/plant_hot_leads.py` plants 30 known hot leads (several buying signals at once) and 16 *decoys* (great on one signal only: a huge deal but unreachable, lost to a competitor, only opens emails, a slipped deal). "Truly hot" = planted hot leads + any organic lead passing the same sales-manager checklist. The strict number counts planted leads only.

**Sample data:** 2,246 raw leads → 2,050 clean leads (196 duplicates merged, 12 possible duplicates for review), 1,437 deals, 7,672 activities, 1,234 call notes.

## 11. Testing

```bash
python eval/demo_check.py         # clicks through the whole demo headlessly – must end "DEMO READY"
python eval/test_data_layer.py    # 43 independent checks: cleaning, dedupe, upload validation, end-to-end
python eval/test_ai_layer.py      # 29 checks: SQL safety, citations, fallbacks, routing (--live 15 with a key)
python eval/eval_ranking.py       # precision@20
python eval/eval_dedupe.py        # recall / precision / false merges (--seeds 10 for robustness)
python eval/eval_qa.py            # 50-question text-to-SQL + citation evaluation (needs an API key)
```
The tests were verified to catch real bugs: each safeguard was removed on purpose and the matching test failed. They pass on Python 3.10 (pandas 2.3) and Python 3.12 (pandas 3.0, the Streamlit Cloud default).

## 12. Repository layout

```
app.py                     Streamlit UI (5 tabs)
core/
  ingest.py                upload validation and header mapping
  clean.py                 cleaning, entity resolution, health report
  scoring.py               decision agent: signals, weights, presets, suggested action
  filters.py               plain-English queue filters (LLM + rules)
  qa.py                    DuckDB store, router, text-to-SQL, fallbacks
  │   └─ call-note search (TF-IDF or          │   │   suggest_action()                        │
  │      embeddings), cites note IDs          │   │                                           │
  citations.py             citation checker
  actions.py               explanations, email drafts, audit log
  llm.py                   OpenAI-compatible client with timeouts and circuit breaker
data/
  leads.csv deals.csv activity.csv notes.csv    sample CRM (messy on purpose)
  generate_data.py         regenerates the sample CRM + truth_duplicates.csv
  plant_hot_leads.py       plants known hot leads + decoys, writes truth_hot_leads.csv
eval/                      evaluation scripts, test sets, results reports, test suites
.streamlit/                theme + secrets.toml.example
INSTRUCTIONS.md            how to use, upload and deploy
```

## 13. Quick start

```bash
git clone https://github.com/RedRumRex/buildfastwithai-hack.git
cd buildfastwithai-hack
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
streamlit run app.py
```
Open http://localhost:8501. The app works without an API key; to switch on the AI, add a key as described in **[INSTRUCTIONS.md](INSTRUCTIONS.md)**, which also covers uploading your own CSVs and deploying to Streamlit Cloud.

## 14. Team

**Team Evex** · Thapar Institute of Engineering & Technology · final year

| Member | Role | Owned |
|---|---|---|
| **Krish Kumar** | Machine Learning · AI / LLM Engineer | LLM client, text-to-SQL agent, RAG, citation checker, QA evaluation — `core/llm.py`, `core/qa.py`, `core/rag.py`, `core/citations.py`, *Ask your data* tab, `eval/eval_qa.py` |
| **Yashraj Sharma** | Backend · Data Cleaning Engineer | Sample data, entity resolution, upload validation, dedupe evaluation, deployment — `data/generate_data.py`, `core/clean.py`, `core/ingest.py`, *Data health* tab, `eval/eval_dedupe.py`, Streamlit Cloud |
| **Purunjay Bhardwaj** | Frontend · Decision & Product Engineer | Scoring rubric, Streamlit UI, approval & audit flow — `core/scoring.py`, `core/filters.py`, `app.py`, `core/actions.py` (audit), `eval/eval_ranking.py` |

**Thank you · Try it live → [leadlens.streamlit.app](https://leadlens.streamlit.app)**
