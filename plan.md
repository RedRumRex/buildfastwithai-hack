# LeadLens: Project Plan

**AI Build Challenge 2026 · PS-04: AI Decision Engine for Business Data**
**Team:** [Team name] · 3 members · **Final build due: 1 October 2026**

---

## 1. Main goal

Build an AI decision engine that helps a **B2B sales team** answer one question:

> **"Who should we contact this week, and why?"**

The system must:

1. **Clean messy business data.** Merge duplicate leads and flag stale or unreachable contacts.
2. **Answer questions in plain English** using the company's own data (RAG + analytics).
3. **Decide which leads to contact first**, and suggest the next action for each one.
4. **Trace every answer and decision** back to the exact rows of data behind it.
5. **Keep a human in control.** Nothing happens until a person approves it, and every decision is logged.

**Guiding principle:** *the math decides, the AI explains, the human approves.*
The LLM never makes up a score or a number. Scores come from explicit rules, and numbers come from executed SQL.

### What "done" looks like on 1 October
- A working web app, deployed with a public link, that runs the full demo end to end
- It works on sample data **and** on uploaded CSVs
- The LLM is connected (Groq or Gemini), with rule-based fallbacks so the demo can never break
- Evaluation numbers we can show: duplicate detection accuracy, text-to-SQL accuracy, citation validity
- A 3-minute demo video, and a README anyone can run

---

## 2. How we will achieve it

| Step | What happens | How |
|---|---|---|
| 1. Ingest | Load leads, deals, activity and call notes | CSV upload or sample data |
| 2. Clean | Merge duplicates, flag stale records, build a health report | Normalisation + fuzzy matching (RapidFuzz), merge log |
| 3. Store | Put clean data somewhere we can query it | In-memory DuckDB (SQL) + a notes search index |
| 4. Score | Rank every lead | Transparent 7-part rubric; every point stores its source row IDs |
| 5. Explain | Write a short reason for each top lead | LLM, given only the scored facts |
| 6. Ask | Answer free-form questions | Router → text-to-SQL (numbers) or RAG over call notes (what customers said) |
| 7. Approve | A rep approves, edits or rejects each suggested action | UI buttons; an approval triggers an email draft |
| 8. Audit | Record every decision | Audit log with reviewer, time, action and evidence IDs |

---

## 3. Architecture

```
                ┌──────────────────────── DATA LAYER (Part 1) ────────────────────────┐
 leads.csv ─┐   │                                                                      │
 deals.csv ─┤   │  generate_data.py ──►  clean.py                                      │
 activity ──┼──►│  (sample data)        normalise → dedupe → re-link → stale flags    │
 notes.csv ─┘   │                        → health report + merge log                   │
                └───────────────────────────────┬──────────────────────────────────────┘
                                                │ CleanResult (clean tables)
                ┌───────────────────────────────▼──────────────────────────────────────┐
                │                    DuckDB (in-memory) + notes index                  │
                └───────┬───────────────────────────────────────────┬──────────────────┘
                        │                                           │
   ┌──── AI LAYER (Part 2) ─────────────┐     ┌──── DECISION + APP LAYER (Part 3) ────────┐
   │ qa.py                              │     │ scoring.py                                │
   │  router ─► text-to-SQL agent       │     │  7-part rubric → score + evidence IDs     │
   │        │   (read-only guard, retry)│     │  suggest_action()                         │
   │        └─► RAG over call notes     │     │                                           │
   │ llm.py (Groq / Gemini / OpenAI)    │◄────┤ app.py (Streamlit UI)                     │
   │ actions.py: explanations,          │     │  Data health · Action queue · Ask ·       │
   │             email drafts           │────►│  Audit log · How scoring works            │
   └────────────────────────────────────┘     │  Approve / Reject ─► audit_log.csv        │
                                              └───────────────────────────────────────────┘
```

### Repository layout
```
leadlens/
├── app.py                 # Streamlit UI (Part 3)
├── core/
│   ├── clean.py           # cleaning + dedupe + health (Part 1)
│   ├── scoring.py         # decision agent (Part 3)
│   ├── qa.py              # analytics agent + RAG (Part 2)
│   ├── actions.py         # explanations, email drafts, audit log (Part 2 + Part 3)
│   └── llm.py             # LLM client (Part 2)
├── data/
│   └── generate_data.py   # synthetic messy CRM (Part 1)
├── eval/                  # evaluation scripts + test sets (all parts)
├── requirements.txt
├── README.md
└── plan.md
```

### Interfaces between parts (agree on these first, then don't change them without telling the team)

| Interface | Owner | Used by |
|---|---|---|
| `clean(leads, deals, activity, notes) -> CleanResult` with `.leads .deals .activity .notes .merges .health .ref_date` | Part 1 | Parts 2 and 3 |
| CSV column names (same as the files in `data/`) | Part 1 | everyone |
| `score_leads(...) -> DataFrame` where each row has `components = [{component, points, fact, source_table, source_ids}]` | Part 3 | Part 2 (explanations) |
| `answer(store, question) -> {type, answer, sql, table, engine}` | Part 2 | Part 3 (UI) |
| `explain(row) -> str`, `draft_email(row, action) -> (subject, body)` | Part 2 | Part 3 (UI) |

---

## 4. Tech stack

| Area | Choice | Why |
|---|---|---|
| Language | Python 3.11 | One language for data, AI and UI |
| UI | Streamlit | Fastest route to a polished, interactive demo |
| Database | DuckDB (in-memory) | Fast SQL on dataframes with no server to run |
| Data cleaning | pandas, RapidFuzz | Fuzzy name and company matching |
| Sample data | Faker | Realistic synthetic CRM with planted errors |
| LLM | OpenAI-compatible API: **Groq (Llama 3.3 70B)** or **Gemini Flash** | Free tiers, fast, swappable through `.env` |
| RAG | TF-IDF today → **sentence-transformers embeddings + Chroma** (upgrade) | Better semantic search over call notes |
| Agents | Plain tool-calling in Python (router, SQL agent, decision agent) | Simple, transparent, easy to debug |
| Deployment | Streamlit Community Cloud (or Hugging Face Spaces) | Free public link for the judges |
| Collaboration | GitHub: one repo, feature branches, pull requests into `main` | Avoids overwriting each other |

---

## 5. Work split

A working prototype already exists, so each member **owns one layer**, improves it, and proves it works with evaluation numbers. The three parts are independent as long as everyone keeps the interfaces in section 3.

### 👤 Member 1: Data & Cleaning Engineer
**Owns:** `data/generate_data.py`, `core/clean.py`, the Data health tab, CSV upload, data evaluation

**Tasks**
1. **Richer sample data:** more realistic messiness, such as company renames, conflicting phone numbers or titles, and typos in emails. Record the ground truth for every injected duplicate (for example `data/truth_duplicates.csv`) so we can measure accuracy.
2. **Improve entity resolution:** catch the 3 duplicates we miss today without adding false merges. Add a "possible duplicate: needs review" band for borderline matches.
3. **Upload robustness:** check column names on uploaded CSVs, show a clear error for missing or renamed columns, and accept common variants (for example `Email` vs `email`).
4. **Data health tab:** add a before/after summary and field-level issues (missing phone, invalid email format).
5. **Evaluation (`eval/eval_dedupe.py`):** report precision, recall and false-merge count against the ground truth.

**Deliverables:** updated generator + ground-truth file, improved `clean.py`, upload validation, dedupe evaluation report
**Target metric:** duplicate recall ≥ 98%, zero false merges

---

### 👤 Member 2: AI / LLM Engineer
**Owns:** `core/llm.py`, `core/qa.py`, the LLM parts of `core/actions.py`, the Ask your data tab, AI evaluation

**Tasks**
1. **Connect the LLM:** set up Groq or Gemini through `.env`, and handle timeouts and rate limits gracefully (fall back to rules on failure).
2. **Text-to-SQL agent:** tune the system prompt and schema description, add 3–5 few-shot examples, and keep the read-only SQL guard and the retry-on-error loop.
3. **Upgrade RAG:** replace TF-IDF in `DataStore.search_notes` with embeddings (sentence-transformers) + Chroma, keeping the same function signature.
4. **Grounded explanations and emails:** the LLM uses only the facts it is given, and cites IDs like `[D00123]`. Add a check that every cited ID really exists in the data, and drop or flag any that don't.
5. **Evaluation (`eval/eval_qa.py`):** build a set of **50 test questions** with hand-written correct SQL. Report execution accuracy (does the result match?), citation validity (% of cited IDs that exist), and fallback rate.

**Deliverables:** working LLM mode, embedding-based RAG, citation checker, 50-question test set + results
**Target metric:** ≥ 85% of test questions answered correctly, 100% of cited IDs valid

---

### 👤 Member 3: Decision Engine & Product Engineer
**Owns:** `core/scoring.py`, `app.py`, the approval + audit flow, deployment, demo

**Tasks**
1. **Decision agent:** review the scoring rubric, and let the manager adjust the weights from the "How scoring works" tab, with the score updating live. Keep every point tied to source IDs.
2. **Ranking evaluation:** coordinate with Member 1 to plant known "hot" leads in the sample data, then measure **precision@20** (how many of the top 20 are truly hot leads).
3. **Action queue UX:** improve the plain-English filter parser (let the LLM parse it when available, with the regex parser as fallback), add bulk approve/reject, and let the user edit and download email drafts.
4. **Audit log:** capture every approve, reject, edit and undo, and add a filterable view plus CSV export.
5. **Ship it:** deploy to Streamlit Cloud, write the final README, record the **3-minute demo video**, and update the pitch deck with final screenshots and numbers.

**Deliverables:** tuned scoring + ranking evaluation, polished UI, deployed public link, demo video, final deck
**Target metric:** precision@20 ≥ 80%, and the full demo runs with zero errors

---

## 6. Timeline (27 Sep → 1 Oct)

| Day | Member 1 (Data) | Member 2 (AI) | Member 3 (Decision + App) |
|---|---|---|---|
| **Sat 27 Sep** | Set up the repo, agree on interfaces, everyone runs the prototype locally | same | same |
| **Sun 28 Sep** | Ground-truth file, richer messiness | LLM connected, SQL prompt + few-shots | Planted hot leads (with M1), editable weights |
| **Mon 29 Sep** | Dedupe improvements + upload validation | Embeddings RAG, citation checker | Action queue UX, audit log view |
| **Tue 30 Sep** | Dedupe evaluation report | 50-question evaluation | Deploy, ranking evaluation |
| **Wed 1 Oct** | **Integration + bug bash (all):** run the full demo 5+ times, record the demo video, final README and deck, submit | | |

**Daily 15-minute sync (evening):** what I finished, what I'm doing next, anything blocking me.

---

## 7. Working rules

- **Branches:** `data/*`, `ai/*`, `app/*`. Open a pull request into `main`; someone else glances at it before merging.
- **Never break the demo:** `main` must always run (`streamlit run app.py`). Test before you merge.
- **Interfaces are a contract:** if you need to change one from section 3, tell the team first.
- **Secrets:** API keys only in `.env`, never committed (it is already in `.gitignore`).
- **Fallbacks stay:** every AI feature must still work (in simpler form) when the LLM is unavailable.

## 8. Risks and backups

| Risk | Backup |
|---|---|
| LLM API down or rate-limited during the demo | Rule-based fallbacks already built in; switch the provider in `.env` |
| Live demo fails | Pre-recorded demo video + screenshots in the deck |
| Integration breaks late | Freeze features on 30 Sep evening; 1 Oct is for fixes only |
| A member falls behind | Cut in this order: bulk approve → editable weights → embeddings RAG (keep TF-IDF). **Never cut** the "Why?" evidence trail or the approval step. |
