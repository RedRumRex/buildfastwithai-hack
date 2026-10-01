# LeadLens — Instructions

How to use LeadLens, upload your own data, run it on your computer and deploy it to Streamlit Cloud.
For what LeadLens is and how it works, see the [README](README.md).

## Contents
1. [Using the app in 5 minutes](#1-using-the-app-in-5-minutes)
2. [Uploading your own data](#2-uploading-your-own-data)
3. [The 4 CSV files — exact structure](#3-the-4-csv-files--exact-structure)
4. [Secrets: where the API key goes](#4-secrets-where-the-api-key-goes)
5. [Deploying on Streamlit Community Cloud](#5-deploying-on-streamlit-community-cloud)
6. [Running on your own computer](#6-running-on-your-own-computer)
7. [Using each tab](#7-using-each-tab)
8. [Troubleshooting](#8-troubleshooting)
9. [Before a demo](#9-before-a-demo)

---

## 1. Using the app in 5 minutes

1. Open the app. It starts on the **Sample CRM** — a deliberately messy dataset of 2,246 lead records.
2. ** Data health** — see what was cleaned: 196 duplicates merged, stale leads flagged, the merge log.
3. ** Action queue** — the ranked list of who to contact. Open the first card, read *Why this lead*, switch on **🔍 Why?** to see the source rows.
4. Type in **Refine in plain English**: `skip anyone contacted in the last 2 weeks, FinTech only, over $20k`.
5. ** Approve** a lead → an email draft appears. Edit it, ** Save edits**, ** Download .eml**.
6. ** Ask your data** — click *Which deals over $50k are stuck?* (shows the SQL) and *Who complained about pricing?* (cites call notes).
7. ** Audit log** — every decision you just made, with its evidence.
8. ** How scoring works** — drag a slider and watch the queue re-rank.

The sidebar shows **"LLM connected"** when an API key is set. Without one, everything still works using built-in rules.

## 2. Uploading your own data

1. In the sidebar, choose **Data source → Upload my CSVs**.
2. Four upload boxes appear. Upload **one file in each box**:

   | Box | File | What it contains |
   |---|---|---|
   | `leads.csv` | your contacts | one row per person / lead |
   | `deals.csv` | your opportunities | one row per deal, linked to a lead |
   | `activity.csv` | engagement events | email opens, calls, meetings, demo requests… |
   | `notes.csv` | call notes | free-text notes from sales calls |

   The file names don't have to match — what matters is which box you put each file in.
3. LeadLens checks the files as soon as all four are uploaded:
   - **"All 4 files passed the checks."** → the whole app now runs on your data. Expand **"N note(s) about your files"** to see which column names were recognised and any small fixes (e.g. a value that isn't a date was left empty).
   - **A red error** → the upload can't be used yet. The message names the file and the problem (e.g. `leads.csv: missing required column(s): email`). The app keeps showing the sample data until you fix it and upload again.
4. To go back, choose **Sample CRM (messy on purpose)** in the sidebar.

**Try it:** the folder `test_upload/` (if present in the repo) contains a ready-made messy export — 315 leads with 15 hidden duplicates and 8 planted hot leads (`CRM-0001` … `CRM-0008`). Upload `leads.csv`, `deals.csv`, `activity.csv` and `notes.csv` from it; you should see 15 duplicates merged and the 8 hot leads at the top of the queue. (`_answer_key_duplicates.csv` is the answer key — don't upload it.)

> **Privacy:** uploaded files are processed in the app's memory and are not saved as files. Two things do leave memory: the **audit log** (the decisions you make — lead name, company, action, email draft) is written to the server's disk, and when an API key is set, the **scored facts, questions and relevant call notes** are sent to the LLM provider. Don't upload real customer data to a public demo.

## 3. The 4 CSV files — exact structure

**General rules**
- Plain CSV with a header row, UTF-8. Extra columns are allowed and ignored.
- **Column names are flexible:** matching ignores upper/lower case, spaces and punctuation, and common alternatives are recognised (listed below). `E-mail`, `email`, `Email Address` all work.
- **Dates:** `YYYY-MM-DD` is safest; most common formats (`2026-09-30`, `30/09/2026`, `Sep 30 2026`) are understood. If more than half of a required date column isn't dates, the upload is rejected; a few bad values are left empty with a note.
- **IDs:** every row needs an ID. Rows without one are skipped; repeated IDs keep the first row. IDs can be any text (`L00001`, `CRM-0042`, `12345`).
- **Links:** `lead_id` in deals, activity and notes must match a `lead_id` in leads. Rows pointing to an unknown lead are reported (and get no lead to attach to).
- **Required** = the column must exist (values may still be blank where noted). **Optional** columns are filled with a default if missing.

### `leads.csv` — one row per contact

| Column | Required | Format / allowed values | Also accepted as | Example |
|---|---|---|---|---|
| `lead_id` | ✅ | unique text | id, leadid, lead, lead_no, lead_number, contact_id | `L00001` |
| `name` | ✅ | text | full_name, contact_name, lead_name, contact, person | `Priya Sharma` |
| `company` | ✅ | text | company_name, account, account_name, organisation, organization, org | `Acme Inc.` |
| `email` | ✅ (values may be blank) | email address | email_address, e_mail, mail, work_email | `priya@acme.com` |
| `created_at` | ✅ | date | created, created_date, date_created, created_on | `2025-03-14` |
| `last_contact_date` | ✅ (values may be blank) | date | last_contacted, last_contact, last_contacted_on, last_touch | `2026-09-20` |
| `phone` | optional | text | phone_number, mobile, telephone, tel, contact_number | `+91 98765 43210` |
| `title` | optional | text | job_title, designation, position, role | `CTO` |
| `industry` | optional | text — **FinTech, Healthcare, E-commerce, Logistics** earn fit points | — | `FinTech` |
| `company_size` | optional (default 0) | whole number of employees | employees, employee_count, size, headcount, no_of_employees | `250` |
| `country` | optional | text | — | `India` |
| `source` | optional | text | — | `Website` |
| `email_status` | optional (default `valid`) | `valid`, `bounced` or `invalid` | email_state | `valid` |

### `deals.csv` — one row per opportunity

| Column | Required | Format / allowed values | Also accepted as | Example |
|---|---|---|---|---|
| `deal_id` | ✅ | unique text | id, dealid, opportunity_id, opp_id | `D00001` |
| `lead_id` | ✅ | must match a lead | lead, leadid, contact_id | `L00001` |
| `amount_usd` | ✅ | number; `$12,000` and `12000` both work | amount, deal_amount, value, deal_value, amount_in_usd | `48000` |
| `stage` | ✅ | `New`, `Qualified`, `Demo Scheduled`, `Proposal Sent`, `Negotiation`, `Closed Won`, `Closed Lost` (any case / spacing) | deal_stage, pipeline_stage, status | `Negotiation` |
| `expected_close_date` | ✅ | date | close_date, expected_close, closing_date | `2026-10-25` |
| `last_stage_change` | ✅ | date | stage_changed, stage_change_date, last_stage_update, stage_updated | `2026-09-01` |
| `deal_name` | optional (defaults to the deal ID) | text | name, opportunity, opportunity_name, title | `Acme – Platform` |

A `deals.csv` with only the header row is accepted (no lead then gets deal points).

### `activity.csv` — one row per engagement event

| Column | Required | Format / allowed values | Also accepted as | Example |
|---|---|---|---|---|
| `activity_id` | ✅ | unique text | id, event_id | `A000001` |
| `lead_id` | ✅ | must match a lead | lead, leadid, contact_id | `L00001` |
| `type` | ✅ | `email_open`, `email_click`, `pricing_page_visit`, `call`, `meeting`, `demo_request` (`Demo Request`, `Pricing Page Visit` etc. also work) | activity_type, event_type, event, kind | `meeting` |
| `activity_date` | ✅ | date | date, event_date, timestamp, occurred_at | `2026-09-28` |

Unknown activity types are kept but earn no engagement points. A `call` or `meeting` also counts as a contact for recency.

### `notes.csv` — one row per call note

| Column | Required | Format | Also accepted as | Example |
|---|---|---|---|---|
| `note_id` | ✅ | unique text | id, noteid | `N00001` |
| `lead_id` | ✅ | must match a lead | lead, leadid, contact_id | `L00001` |
| `note_date` | ✅ | date | date, created_at, created, note_created | `2026-09-15` |
| `text` | ✅ | free text | note, notes, body, content, note_text, comment | `Budget approved, wants a proposal.` |
| `author` | optional | text | owner, rep, written_by, created_by | `Rohan (AE)` |

Call-note signals are keyword-based. Phrases that count as **buying signals**: *budget approved, pilot, proposal, renewal, add seats, enterprise pricing, strong buying signal, actively evaluating, champion*. Phrases that count as **objections**: *went with a competitor, no response, on hold, not the right contact, budget is tight, pricing concerns, steep discount*.

### Minimal valid example

`leads.csv`
```csv
lead_id,name,company,email,created_at,last_contact_date,industry,company_size
L1,Priya Sharma,Acme Inc.,priya@acme.com,2025-01-10,2026-09-25,FinTech,250
L2,Rohan Mehta,Beta Ltd,rohan@beta.com,2025-03-02,2026-06-01,Retail,40
```
`deals.csv`
```csv
deal_id,lead_id,amount_usd,stage,expected_close_date,last_stage_change
D1,L1,"$60,000",Negotiation,2026-10-15,2026-09-20
```
`activity.csv`
```csv
activity_id,lead_id,type,activity_date
A1,L1,demo_request,2026-09-26
A2,L2,email_open,2026-09-10
```
`notes.csv`
```csv
note_id,lead_id,note_date,author,text
N1,L1,2026-09-24,Rohan (AE),"Budget approved, wants a proposal by Friday."
```

**What "today" means:** LeadLens measures recency from the day after the latest activity or contact date in your data, so an old export still ranks sensibly.

### Upload messages explained

| Message | Meaning | What to do |
|---|---|---|
| `missing required column(s): …` | a required column isn't there under any accepted name | rename the column or add it |
| `could not be read as a CSV file` | not a CSV, or broken encoding | re-export as CSV (UTF-8) |
| `has no data rows` | only a header (allowed for deals only) | add rows |
| `column '…' is mostly not dates` | > 50 % of a date column can't be read | use `YYYY-MM-DD` |
| `no usable activity_date or last_contact_date` | no dates at all to measure recency from | fill in at least some dates |
| `read 'E-mail' as 'email'` (note) | a header variant was recognised | nothing |
| `… value(s) … are not dates and were left empty` (note) | a few bad dates | optional: fix at the source |
| `… row(s) point to a lead_id that is not in leads.csv` (note) | orphan deals/activity/notes | optional: check your IDs |
| `Your files passed the checks but could not be processed` | an unexpected problem deeper in the pipeline | the app shows sample data; report the message |

## 4. Secrets: where the API key goes

LeadLens works with **any OpenAI-compatible API**. The team deploys with **Groq** (free tier). The key is never in the code or the repository.

| Where you run LeadLens | Where the key goes | Format |
|---|---|---|
| **Streamlit Cloud** | App → **Settings → Secrets** | TOML: `NAME = "value"` (quotes required) |
| **Your computer** | a file named `.env` in the project folder | `NAME=value` (no quotes, no spaces) |

**Streamlit Cloud → Settings → Secrets** — paste exactly:
```toml
LLM_API_KEY = "gsk_your_groq_key_here"
LLM_BASE_URL = "https://api.groq.com/openai/v1"
LLM_MODEL = "openai/gpt-oss-120b"
```

**Local `.env`** (create it next to `app.py`):
```
LLM_API_KEY=gsk_your_groq_key_here
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=openai/gpt-oss-120b
```

**Other providers** (change only these lines):

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | Key from |
|---|---|---|---|
| Groq (default) | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` | console.groq.com → API Keys |
| OpenAI | *(leave the line out)* | `gpt-4o-mini` | platform.openai.com → API keys |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` | aistudio.google.com → Get API key |

> The reported 94% text-to-SQL accuracy was measured with Groq `openai/gpt-oss-120b`. With another model, re-run `python eval/eval_qa.py` before quoting a number.

**Optional settings** (same place, same format):

| Name | Default | Meaning |
|---|---|---|
| `LLM_TIMEOUT` | `20` | seconds before an LLM request is abandoned |
| `LLM_COOLDOWN` | `60` | seconds the app uses rules only after repeated LLM failures |
| `LLM_REASONING_EFFORT` | `low` | reasoning effort for `gpt-oss` models |
| `RAG_BACKEND` | `embeddings` | `tfidf` to force keyword search for call notes |
| `RAG_MIN_RELEVANCE` | `0.25` | minimum similarity for embeddings search |
| `EMBED_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | embeddings model (only if installed) |

**Rules:** never commit `.env` or `.streamlit/secrets.toml` (both are in `.gitignore`); never paste a key into the code, the README, an issue or a chat. If a key leaks, revoke it in the provider's console and create a new one.

## 5. Deploying on Streamlit Community Cloud

1. **Push** the code to GitHub (`app.py`, `requirements.txt`, `core/`, `data/` must be on the `main` branch).
2. Go to **share.streamlit.io** → **Continue with GitHub** → **Authorize**. For a private repository, also approve access to private repositories when asked (or make the repository public: repo → Settings → General → Danger Zone → Change visibility).
3. Click **Create app** → deploy from GitHub:
   - **Repository:** `RedRumRex/buildfastwithai-hack`
   - **Branch:** `main`
   - **Main file path:** `app.py`
4. Open **Advanced settings**:
   - **Python version:** `3.12`
   - **Secrets:** paste the three lines from [section 4](#4-secrets-where-the-api-key-goes)
5. Click **Deploy**. The first build takes a few minutes.
6. **Check:** the sidebar shows **"LLM connected: openai/gpt-oss-120b"**.

**After deployment**
- **Change a secret:** app page → **Manage app** (bottom right) → **⋮** → **Settings → Secrets** → edit → **Save**. The app restarts.
- **Updates:** every merge into `main` redeploys automatically. If the app still shows old behaviour after a few minutes: **Manage app → ⋮ → Reboot app**.
- **Logs:** **Manage app** shows the build and run logs, including which commit is running.
- **Sleeping:** free apps sleep after a period without visitors. Open the link a few minutes before a demo.
- **Audit log:** stored on the server, shared by all visitors, reset on restart. Use **Export full log (CSV)** to keep it.

## 6. Running on your own computer

Requires **Python 3.10, 3.11 or 3.12** and git.

```bash
git clone https://github.com/RedRumRex/buildfastwithai-hack.git
cd buildfastwithai-hack
python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
# optional: create .env with your key (section 4)
streamlit run app.py                   # opens http://localhost:8501
```
Each new terminal: `cd` into the folder and run `source .venv/bin/activate` again.

**Mac notes:** use `python3` (there is no `python` until the virtual environment is active). If `pip` says *externally-managed-environment*, you skipped the virtual environment — create and activate it as above.

**Regenerate the sample data** (optional):
```bash
python data/generate_data.py        # new messy CRM + truth_duplicates.csv
python data/plant_hot_leads.py      # re-plant the known hot leads + decoys (always run after generating)
```

## 7. Using each tab

### 🩺 Data health
Read top to bottom: headline numbers → **Before → after cleaning** → **Field-level issues** (example IDs can be looked up in *Browse cleaned leads*) → **Duplicate records we merged** (rule + evidence for each merge) → **Why records are stale** → **Possible duplicates – needs review** (similar records that were *not* merged; check them in your CRM).

### Action queue
- **Plain-English filter** examples:
  - `skip anyone contacted in the last 2 weeks`
  - `FinTech and Healthcare only, over $20k`
  - `late-stage deals, top 15`
  - `negotiation only, include stale`
  - `deals above 5 lakh` (lakh / crore understood)

  The chips underneath show what was understood and whether the AI or the rule-based parser did it. You can also set the filters by hand.
- **Approve one lead:** open the card → optionally edit the action and add a note → ** Approve**. An email draft appears.
- **Edit the email:** change subject/body → ** Save edits** (logged) → ** Download .eml** (downloads the last *saved* version). **Download all approved email drafts** gives a zip.
- **Bulk:** select leads (or **Select all**) in *Bulk review*, add an optional note → ** Approve N** / ** Reject N**. Each lead is logged separately.
- **Undo:** ** Undo decision** on any reviewed card.
- **Reviewer name:** set it in the sidebar; it is written to the audit log.

###  Ask your data
Click an example or type a question.
- **Numbers / lists** ("How many open deals do we have?", "Which Negotiation deals close this month?") → answer + **SQL** + result rows.
- **What customers said** ("Who complained about pricing?", "Which customers mentioned a competitor?") → answer citing note IDs + the source notes.
- The **engine** line says what answered: `text-to-SQL · model`, `RAG (tfidf) · model`, or `built-in query templates` / `retrieval` when the AI is off.

###  Audit log
Filter by decision, reviewer, free text (lead ID, company, note…), date range or **Only bulk decisions**. **Export filtered** or **Export full log** as CSV.

###  How scoring works
Pick a **preset** → **Apply preset**, or drag sliders; **↺ Reset to default** restores the defaults. *Engagement event weights* (expander) sets how much each activity type counts. The **Live effect on the ranking** table shows how the top 15 moved. With the sample data, the precision@20 metric updates live.

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| Sidebar says **"No LLM key set"** | Add the secrets (section 4). On Streamlit Cloud, save and wait for the restart; locally, restart `streamlit run`. |
| AI answers stopped, engine says *built-in* | The LLM hit a rate limit or timeout; the app pauses it for ~60 s and uses rules. Wait and retry, or check the key / credits. |
| Upload shows a red error | Read the message — it names the file and column. See [section 3](#upload-messages-explained). |
| *"Files passed the checks but could not be processed"* | Make sure the deployed app runs the latest `main` (**Manage app → Reboot app**). If it persists, share the message with the team. |
| Hosted app shows old behaviour after a merge | **Manage app → ⋮ → Reboot app**. |
| `zsh: command not found: python` | Use `python3`, or activate the virtual environment first. |
| `externally-managed-environment` | Create and activate `.venv` (section 6). |
| Bulk approve is slow | With the AI on, an email is written for each lead (~4 s each). Approve fewer at once, or accept the wait. |
| Audit log is empty after a while on Streamlit Cloud | The server restarted; export the log regularly. |

## 9. Before a demo

```bash
python eval/demo_check.py          # must end with "13/13 … DEMO READY"
python eval/test_data_layer.py     # 0 failed
python eval/test_ai_layer.py       # 0 failed (add --live 15 to test the real LLM)
python eval/eval_ranking.py        # PASS
```
- Delete `data/audit_log.csv` if you demo locally, so the audit log starts empty.
- Switch the sidebar to **Sample CRM** — the demo numbers (196 duplicates, 95% precision@20) are for the sample data.
- Open the hosted link a few minutes early to wake it up, and check **"LLM connected"**.
