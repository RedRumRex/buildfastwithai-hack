"""
Analytics agent + RAG over notes.

Question router:
  - numeric / structured questions  -> text-to-SQL over DuckDB (the SQL is shown to the user)
  - "what did customers say" style  -> retrieval over call notes (note ids are cited)

Numbers ALWAYS come from executed SQL, never from the LLM's imagination.
"""
import re

import duckdb
import pandas as pd

from . import llm
from .citations import check_citations
from .rag import NotesIndex

SCHEMA = """
Tables (DuckDB SQL):
leads(lead_id, name, company, email, phone, title, industry, company_size INT, country, source,
      created_at DATE, last_contact_date DATE, email_status ['valid','bounced','invalid'], merged_from, n_sources INT,
      days_since_contact INT, is_stale BOOLEAN, stale_reason)
deals(deal_id, lead_id, deal_name, amount_usd INT, stage, expected_close_date DATE, last_stage_change DATE, original_lead_id)
   stage values: 'New','Qualified','Demo Scheduled','Proposal Sent','Negotiation','Closed Won','Closed Lost'
   open deals = stage NOT IN ('Closed Won','Closed Lost')
activity(activity_id, lead_id, type, activity_date DATE, original_lead_id)
   type values: 'email_open','email_click','pricing_page_visit','call','meeting','demo_request'
notes(note_id, lead_id, note_date DATE, author, text, original_lead_id)
lead_scores(lead_id, rank INT, score DOUBLE, name, company, industry, top_deal_id, top_deal_amount, top_deal_stage, is_stale)
merges(duplicate_id, kept_id, rule, evidence, match_score)
Join everything on lead_id. Today's date for this dataset is {ref}.
"""

NOTES_HINTS = ["said", "mention", "complain", "concern", "worried", "note", "feedback", "competitor",
               "objection", "asked about", "interested in", "told", "call notes", "what are customers"]

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|copy|pragma|install|load|export|call)\b", re.I)


class DataStore:
    """Holds the cleaned data in an in-memory DuckDB database + a notes search index."""

    def __init__(self, clean_result, scores: pd.DataFrame):
        self.ref_date = clean_result.ref_date
        self.con = duckdb.connect(":memory:")
        leads = clean_result.leads.copy()
        leads["created_at"] = leads["created_at"].dt.date
        leads["last_contact_date"] = leads["last_contact_date"].dt.date
        leads = leads.drop(columns=["master_id", "last_activity_date"], errors="ignore")
        deals = clean_result.deals.copy()
        for c in ["expected_close_date", "last_stage_change"]:
            deals[c] = deals[c].dt.date
        act = clean_result.activity.copy()
        act["activity_date"] = act["activity_date"].dt.date
        notes = clean_result.notes.copy()
        notes["note_date"] = notes["note_date"].dt.date
        sc = scores.drop(columns=["components", "email", "title", "company_size", "days_since_contact", "merged_from"], errors="ignore")
        for name, df in [("leads", leads), ("deals", deals), ("activity", act), ("notes", notes),
                         ("lead_scores", sc), ("merges", clean_result.merges)]:
            self.con.register(f"_{name}", df)
            self.con.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
            self.con.unregister(f"_{name}")
        # SECURITY: the data is loaded, so cut DuckDB off from the file system / network for good. Without this a
        # plain SELECT (e.g. an LLM-written query after prompt injection) can read files on the server –
        # read_text('.streamlit/secrets.toml') would show the API key. lock_configuration stops SQL undoing it.
        self.con.execute("SET enable_external_access = false")
        self.con.execute("SET lock_configuration = true")

        # notes retrieval index: embeddings + Chroma (auto-falls back to TF-IDF)
        self.notes = notes.merge(leads[["lead_id", "name", "company"]], on="lead_id", how="left")
        self.index = NotesIndex(self.notes)
        self.rag_engine = self.index.engine
        self._known_ids = None

    # ---------------- SQL -----------------
    def run_sql(self, sql: str, limit: int = 200) -> pd.DataFrame:
        sql = sql.strip().rstrip(";")
        if ";" in sql or FORBIDDEN.search(sql) or not re.match(r"^\s*(select|with)\b", sql, re.I):
            raise ValueError("Only a single read-only SELECT query is allowed.")
        df = self.con.execute(f"SELECT * FROM ({sql}) AS q LIMIT {limit}").df()
        for c in df.columns:  # show pure dates without 00:00:00
            if pd.api.types.is_datetime64_any_dtype(df[c]) and (df[c].dropna().dt.normalize() == df[c].dropna()).all():
                df[c] = df[c].dt.date
        df.attrs["total_rows"] = self.con.execute(f"SELECT COUNT(*) FROM ({sql}) AS q").fetchone()[0]
        return df

    def schema(self) -> str:
        return SCHEMA.format(ref=self.ref_date.date())

    def known_ids(self) -> set[str]:
        """Every record id that exists in the cleaned data (used by the citation checker)."""
        if self._known_ids is None:
            q = ("SELECT lead_id FROM leads UNION SELECT deal_id FROM deals "
                 "UNION SELECT activity_id FROM activity UNION SELECT note_id FROM notes")
            self._known_ids = {str(r[0]) for r in self.con.execute(q).fetchall() if r[0] is not None}
        return self._known_ids

    # ---------------- Notes RAG -----------------
    def search_notes(self, query: str, k: int = 8) -> pd.DataFrame:
        res = self.index.search(query, k)
        return res[["note_id", "lead_id", "name", "company", "note_date", "author", "text", "relevance"]]


# ---------------------------------------------------------------------------
# Rule-based fallback (works with no API key)
# ---------------------------------------------------------------------------
def _money(q: str):
    q = q.lower().replace(",", "")
    m = (re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(k|m|thousand|lakh)?\b", q)
         or re.search(r"(\d+(?:\.\d+)?)\s*(k|m|thousand|lakh)\b", q)
         or re.search(r"(?:over|above|than|>)\s*(\d{4,})()", q))
    if not m:
        return None
    v = float(m.group(1))
    unit = m.group(2) or ""
    return int(v * {"k": 1e3, "thousand": 1e3, "m": 1e6, "lakh": 1e5}.get(unit, 1))


def rule_sql(q: str) -> tuple[str, str]:
    ql = q.lower()
    OPEN = "stage NOT IN ('Closed Won','Closed Lost')"
    n = re.search(r"\btop\s+(\d+)", ql)
    top = int(n.group(1)) if n else 10
    # ---- composable filters over open deals (e.g. "deals over $50k that are stuck and close this month")
    where, parts = [f"d.{OPEN}"], []
    if "stuck" in ql or "stalled" in ql:
        where.append("date_diff('day', d.last_stage_change, DATE '{ref}') >= 45")
        parts.append("stuck in the same stage 45+ days")
    amt = _money(ql) if any(w in ql for w in ["over", "above", "more than", "greater", ">", "bigger"]) else None
    if amt:
        where.append(f"d.amount_usd > {amt}")
        parts.append(f"worth more than ${amt:,}")
    if "clos" in ql and any(w in ql for w in ["this month", "30 days", "soon", "next month", "this week"]):
        days = 7 if "week" in ql else 30
        where.append(f"d.expected_close_date BETWEEN DATE '{{ref}}' AND DATE '{{ref}}' + INTERVAL {days} DAY")
        parts.append(f"closing in the next {days} days")
    if "overdue" in ql or "slipp" in ql or "past close" in ql:
        where.append("d.expected_close_date < DATE '{ref}'")
        parts.append("past their expected close date")
    for stg in ["negotiation", "proposal sent", "demo scheduled", "qualified"]:
        if stg in ql:
            where.append(f"lower(d.stage) = '{stg}'")
            parts.append(f"in {stg.title()}")
    if parts:
        return ("Open deals " + ", ".join(parts),
                "SELECT d.deal_id, d.lead_id, l.name, l.company, d.stage, d.amount_usd, d.expected_close_date, d.last_stage_change, "
                "date_diff('day', d.last_stage_change, DATE '{ref}') AS days_in_stage FROM deals d JOIN leads l USING(lead_id) "
                "WHERE " + " AND ".join(where) + " ORDER BY d.amount_usd DESC")
    if "industry" in ql:
        return ("Open pipeline by industry",
                f"SELECT l.industry, COUNT(DISTINCT l.lead_id) AS leads, COUNT(d.deal_id) AS open_deals, COALESCE(SUM(d.amount_usd),0) AS open_pipeline_usd "
                f"FROM leads l LEFT JOIN deals d ON d.lead_id = l.lead_id AND d.{OPEN} GROUP BY l.industry ORDER BY open_pipeline_usd DESC")
    if "country" in ql or "region" in ql:
        return ("Leads and pipeline by country",
                f"SELECT l.country, COUNT(DISTINCT l.lead_id) AS leads, COALESCE(SUM(d.amount_usd),0) AS open_pipeline_usd "
                f"FROM leads l LEFT JOIN deals d ON d.lead_id = l.lead_id AND d.{OPEN} GROUP BY l.country ORDER BY open_pipeline_usd DESC")
    if "stage" in ql or "pipeline" in ql or "funnel" in ql:
        return ("Pipeline by stage",
                "SELECT stage, COUNT(*) AS deals, SUM(amount_usd) AS total_value_usd, ROUND(AVG(amount_usd)) AS avg_deal_usd "
                "FROM deals GROUP BY stage ORDER BY total_value_usd DESC")
    if "stale" in ql or "bounced" in ql or "missing" in ql or "dead" in ql:
        return ("Stale or unreachable leads",
                "SELECT lead_id, name, company, email, days_since_contact, stale_reason FROM leads WHERE is_stale ORDER BY days_since_contact DESC")
    if "duplicate" in ql or "merged" in ql:
        return ("Duplicate records that were merged", "SELECT * FROM merges ORDER BY match_score")
    if "won" in ql or "revenue" in ql:
        return ("Closed-won revenue",
                "SELECT COUNT(*) AS won_deals, SUM(amount_usd) AS revenue_usd, ROUND(AVG(amount_usd)) AS avg_deal_usd FROM deals WHERE stage = 'Closed Won'")
    if "lead" in ql and any(w in ql for w in ["top", "best", "priorit", "hot", "contact", "call"]):
        return (f"Top {top} leads by priority score",
                f"SELECT rank, lead_id, name, company, industry, score, top_deal_amount, top_deal_stage FROM lead_scores ORDER BY rank LIMIT {top}")
    if "how many" in ql and "lead" in ql:
        return ("Lead counts", "SELECT COUNT(*) AS total_leads, SUM(CASE WHEN is_stale THEN 1 ELSE 0 END) AS stale_leads FROM leads")
    return ("Open pipeline summary",
            f"SELECT COUNT(*) AS open_deals, SUM(amount_usd) AS open_pipeline_usd, ROUND(AVG(amount_usd)) AS avg_deal_usd FROM deals WHERE {OPEN}")


_NUM = re.compile(r"(?<![A-Za-z\d])\d[\d,]*(?:\.\d+)?")


def _nums(text: str) -> set[float]:
    out = set()
    for m in _NUM.findall(str(text)):
        try:
            out.add(round(float(m.replace(",", "")), 2))
        except ValueError:
            pass
    return out


def ungrounded_numbers(summary: str, df: pd.DataFrame, question: str) -> list[str]:
    """Numbers in an LLM summary that appear neither in the SQL result nor in the question.
    The plan's rule: numbers come from executed SQL, never from the LLM – so such a summary is not shown."""
    allowed = _nums(question) | _nums(df.head(200).to_csv(index=False)) | {float(len(df)), float(df.attrs.get("total_rows", len(df)))}
    for v in df.head(200).select_dtypes("number").to_numpy().ravel():      # rounded variants of real values
        try:
            allowed |= {round(float(v), 2), round(float(v), 1), float(round(float(v))), round(float(v) / 1000, 1),
                        round(float(v) / 1e6, 1), round(float(v) / 1e6, 2)}
        except (TypeError, ValueError):
            pass
    text = re.sub(r"\b[A-Z]{1,3}\d{3,}\b", " ", str(summary))   # record ids are checked by the citation checker
    return [m for m in _NUM.findall(text) if round(float(m.replace(",", "")), 2) not in allowed]


def _fmt(df: pd.DataFrame, n: int = 25) -> str:
    return df.head(n).to_csv(index=False)


# ---------------------------------------------------------------------------
# Text-to-SQL prompt: conventions + few-shot examples (none of them are in eval/qa_testset.json)
# ---------------------------------------------------------------------------
SQL_RULES = """Rules:
- Return ONE read-only DuckDB query (SELECT or WITH). No semicolons, no DDL/DML.
- Use exact stage / type values from the schema. Open deals = stage NOT IN ('Closed Won','Closed Lost').
- Today is DATE '{ref}'. "last N days" = col >= DATE '{ref}' - INTERVAL N DAY.
  "next N days" = col BETWEEN DATE '{ref}' AND DATE '{ref}' + INTERVAL N DAY.
  Days between dates: date_diff('day', start_col, DATE '{ref}').
- "stuck" / "stalled" = open deal with date_diff('day', last_stage_change, DATE '{ref}') >= 45.
- "how many / total / average" questions -> one aggregate row (COUNT/SUM/AVG). Do NOT round unless asked.
- Lists of records: include identifying columns (lead_id or deal_id, name, company) so results are traceable.
- Only add LIMIT when the question asks for a top-N. For "best / priority / who to contact" use lead_scores ORDER BY rank.
- deals/activity/notes join to leads on lead_id. Count distinct leads with COUNT(DISTINCT lead_id)."""

FEW_SHOTS = [
    ("Which deals over $50k are stuck?",
     "SELECT d.deal_id, d.lead_id, l.name, l.company, d.stage, d.amount_usd, date_diff('day', d.last_stage_change, DATE '{ref}') AS days_in_stage "
     "FROM deals d JOIN leads l USING (lead_id) WHERE d.stage NOT IN ('Closed Won','Closed Lost') AND d.amount_usd > 50000 "
     "AND date_diff('day', d.last_stage_change, DATE '{ref}') >= 45 ORDER BY d.amount_usd DESC"),
    ("Revenue won per country",
     "SELECT l.country, COUNT(*) AS won_deals, SUM(d.amount_usd) AS revenue_usd FROM deals d JOIN leads l USING (lead_id) "
     "WHERE d.stage = 'Closed Won' GROUP BY l.country ORDER BY revenue_usd DESC"),
    ("How many email clicks did we get in the last 14 days?",
     "SELECT COUNT(*) AS email_clicks FROM activity WHERE type = 'email_click' AND activity_date >= DATE '{ref}' - INTERVAL 14 DAY"),
    ("Best 5 leads to call this week",
     "SELECT rank, lead_id, name, company, score, top_deal_id, top_deal_amount, top_deal_stage FROM lead_scores ORDER BY rank LIMIT 5"),
    ("Bounced leads that still have an open deal",
     "SELECT DISTINCT l.lead_id, l.name, l.company, l.email FROM leads l JOIN deals d USING (lead_id) "
     "WHERE l.email_status = 'bounced' AND d.stage NOT IN ('Closed Won','Closed Lost')"),
]

COUNT_START = re.compile(r"^\s*(how many|count|number of|total|what is the total|what's the total|average|what is the average)\b")
STRONG_NOTES = ["said", "mention", "complain", "worried", "concern", "objection", "feedback", "told"]


def route(question: str) -> str:
    ql = question.lower()
    if COUNT_START.search(ql) and not any(h in ql for h in STRONG_NOTES):
        return "sql"
    return "notes" if any(h in ql for h in NOTES_HINTS) else "sql"


def sql_prompt(store) -> str:
    ref = str(store.ref_date.date())
    shots = "\n\n".join(f'Q: {q}\n{{"sql": "{s.replace("{ref}", ref)}", "title": "..."}}' for q, s in FEW_SHOTS)
    return ("You translate business questions into ONE DuckDB SQL query over a CRM database.\n" + store.schema() +
            "\n" + SQL_RULES.replace("{ref}", ref) + "\n\nExamples:\n" + shots)


def llm_sql(store, question: str, attempts: int = 3):
    """LLM -> SQL -> guarded execution, feeding DB errors back for up to `attempts` tries.
    API problems (rate limit, timeout, bad key) raise immediately -> rule fallback."""
    system, last_err, sql = sql_prompt(store), None, ""
    for _ in range(attempts):
        user = f"Question: {question}"
        if last_err:
            user += f"\nYour previous SQL was:\n{sql}\nIt failed with: {last_err}\nFix it."
        out = llm.chat_json(system, user + '\nReturn {"sql": "...", "title": "short description of the result"}')
        sql, title = str(out.get("sql", "")).strip(), out.get("title", "Result")
        try:
            if not sql:
                raise ValueError("empty sql")
            return sql, title, store.run_sql(sql)
        except Exception as e:
            last_err = str(e)[:300]
    raise ValueError(f"SQL failed after {attempts} tries: {last_err}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def answer(store: DataStore, question: str, summarize: bool = True) -> dict:
    use_llm = llm.available()
    no_cite = {"cited": [], "valid": [], "invalid": []}

    # ---------- notes (RAG) ----------
    if route(question) == "notes":
        hits = store.search_notes(question)
        eng = f"retrieval ({store.rag_engine})"
        if hits.empty:
            return {"type": "notes", "answer": "No call notes matched that question.", "table": hits, "sql": None,
                    "engine": eng, "fallback": not use_llm, "citations": no_cite}
        err = None
        if use_llm:
            try:
                ctx = "\n".join(f"[{r.note_id}] {r.note_date} | {r.name} @ {r.company} (lead {r.lead_id}): {r.text}"
                                for r in hits.itertuples())
                ans = llm.chat(
                    "You are a sales analyst. Answer ONLY from the call notes provided. Cite every claim with the note id in "
                    "square brackets exactly as given, e.g. [N00012]. Never invent ids. If the notes don't answer the "
                    "question, say so. Be concise (max 6 bullet points).",
                    f"Question: {question}\n\nCall notes:\n{ctx}")
                ans, rep = check_citations(ans, store.known_ids())
                return {"type": "notes", "answer": ans, "table": hits, "sql": None,
                        "engine": f"RAG ({store.rag_engine}) · {llm.model_name()}", "fallback": False, "citations": rep}
            except Exception as e:
                err = str(e)
        bullets = "\n".join(f"- **{r.name}** ({r.company}): “{r.text}” [{r.note_id}]" for r in hits.itertuples())
        msg = f"Most relevant call notes for your question:\n\n{bullets}"
        if err:
            msg += f"\n\n_(LLM unavailable: {err[:120]})_"
        _, rep = check_citations(msg, store.known_ids())
        return {"type": "notes", "answer": msg, "table": hits, "sql": None, "engine": eng, "fallback": True, "citations": rep}

    # ---------- structured (text-to-SQL) ----------
    ref = str(store.ref_date.date())
    fallback_note = None
    if use_llm:
        try:
            sql, title, df = llm_sql(store, question)
            ans, rep, hidden = None, no_cite, None
            if summarize:
                try:
                    ans = llm.chat(
                        "You are a precise sales analyst. Answer the question using ONLY numbers present in the SQL result. "
                        "Mention ids (lead_id / deal_id) for specific records. 2-5 sentences or short bullets. Never invent data.",
                        f"Question: {question}\nSQL: {sql}\nResult ({len(df)} rows, first 25 shown):\n{_fmt(df)}")
                    ans, rep = check_citations(ans, store.known_ids())
                    bad = ungrounded_numbers(ans, df, question)
                    if bad:
                        ans, hidden = None, (f"_AI summary hidden: it mentioned number(s) not in the query result "
                                             f"({', '.join(bad[:3])}). Showing the result itself._")
                except Exception:
                    ans = None
            if ans is None:
                total = df.attrs.get("total_rows", len(df))
                if len(df) == 1 and len(df.columns) <= 4:
                    ans = f"**{title}:** " + ", ".join(
                        f"{c.replace('_', ' ')} = {v:,.0f}" if isinstance(v, (int, float)) and not isinstance(v, bool)
                        else f"{c} = {v}" for c, v in df.iloc[0].items())
                else:
                    ans = f"**{title}** — **{total:,}** row(s). See the table and SQL below."
                if hidden:
                    ans += "\n\n" + hidden
            return {"type": "sql", "answer": ans, "title": title, "sql": sql, "table": df,
                    "engine": f"text-to-SQL · {llm.model_name()}", "fallback": False, "citations": rep}
        except Exception as e:
            fallback_note = f"LLM query failed ({str(e)[:200]}); used built-in query instead."

    title, sql = rule_sql(question)
    sql = sql.replace("{ref}", ref)
    df = store.run_sql(sql)
    if len(df) == 1 and len(df.columns) <= 4:
        ans = f"**{title}:** " + ", ".join(f"{c.replace('_', ' ')} = {v:,.0f}" if isinstance(v, (int, float)) else f"{c} = {v}"
                                           for c, v in df.iloc[0].items())
    else:
        total = df.attrs.get("total_rows", len(df))
        shown = f" (showing first {len(df)})" if total > len(df) else ""
        ans = f"**{title}** — **{total:,}** record(s) found{shown}. Every row below carries its record id."
        money = [c for c in df.columns if c.endswith("amount_usd")]
        if money and total:
            tot_val = store.con.execute(f"SELECT SUM({money[0]}) FROM ({sql}) q").fetchone()[0]
            ans += f" Combined value: **${tot_val:,.0f}**."
    if fallback_note:
        ans += f"\n\n_{fallback_note}_"
    return {"type": "sql", "answer": ans, "title": title, "sql": sql, "table": df,
            "engine": "built-in query templates", "fallback": True, "citations": no_cite}
