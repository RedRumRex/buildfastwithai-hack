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
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import linear_kernel

from . import llm

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

        # notes retrieval index (TF-IDF – swap for embeddings/Chroma if you have time)
        self.notes = notes.merge(leads[["lead_id", "name", "company"]], on="lead_id", how="left")
        docs = (self.notes["text"] + " " + self.notes["company"].fillna("")).tolist()
        self.vec = TfidfVectorizer(ngram_range=(1, 2), stop_words="english")
        self.mat = self.vec.fit_transform(docs)

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

    # ---------------- Notes RAG -----------------
    def search_notes(self, query: str, k: int = 8) -> pd.DataFrame:
        sims = linear_kernel(self.vec.transform([query]), self.mat).ravel()
        res = self.notes.assign(relevance=sims)
        res = res[res["relevance"] > 0].sort_values(["relevance", "note_date"], ascending=[False, False]).head(k)
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


def _fmt(df: pd.DataFrame, n: int = 25) -> str:
    return df.head(n).to_csv(index=False)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def answer(store: DataStore, question: str) -> dict:
    use_llm = llm.available()
    ql = question.lower()
    route = "notes" if any(h in ql for h in NOTES_HINTS) else "sql"

    # ---------- notes (RAG) ----------
    if route == "notes":
        hits = store.search_notes(question)
        if hits.empty:
            return {"type": "notes", "answer": "No call notes matched that question.", "table": hits, "sql": None, "engine": "retrieval"}
        if use_llm:
            try:
                ctx = "\n".join(f"[{r.note_id}] {r.note_date} | {r.name} @ {r.company} (lead {r.lead_id}): {r.text}" for r in hits.itertuples())
                ans = llm.chat(
                    "You are a sales analyst. Answer ONLY from the call notes provided. Cite every claim with the note id in "
                    "square brackets, e.g. [N00012]. If the notes don't answer the question, say so. Be concise (max 6 bullet points).",
                    f"Question: {question}\n\nCall notes:\n{ctx}")
                return {"type": "notes", "answer": ans, "table": hits, "sql": None, "engine": f"RAG · {llm.model_name()}"}
            except Exception as e:
                err = str(e)
        else:
            err = None
        bullets = "\n".join(f"- **{r.name}** ({r.company}): “{r.text}” [{r.note_id}]" for r in hits.itertuples())
        msg = f"Most relevant call notes for your question:\n\n{bullets}"
        if err:
            msg += f"\n\n_(LLM unavailable: {err[:120]})_"
        return {"type": "notes", "answer": msg, "table": hits, "sql": None, "engine": "retrieval (TF-IDF)"}

    # ---------- structured (text-to-SQL) ----------
    ref = str(store.ref_date.date())
    if use_llm:
        system = ("You translate business questions into ONE DuckDB SQL SELECT query.\n" + store.schema() +
                  "\nRules: read-only SELECT/WITH only; always include identifying columns (lead_id / deal_id, name, company) "
                  "so results are traceable; use DATE literals for today; prefer ORDER BY for rankings; for 'best leads' use lead_scores.")
        last_err = None
        for _ in range(2):
            try:
                user = f"Question: {question}" + (f"\nYour previous SQL failed with: {last_err}. Fix it." if last_err else "")
                out = llm.chat_json(system, user + '\nReturn {"sql": "...", "title": "short description of the result"}')
                sql, title = out["sql"], out.get("title", "Result")
                df = store.run_sql(sql)
                summary = llm.chat(
                    "You are a precise sales analyst. Answer the question using ONLY numbers present in the SQL result. "
                    "Mention ids (lead_id / deal_id) for specific records. 2-5 sentences or short bullets. Never invent data.",
                    f"Question: {question}\nSQL: {sql}\nResult ({len(df)} rows, first 25 shown):\n{_fmt(df)}")
                return {"type": "sql", "answer": summary, "title": title, "sql": sql, "table": df, "engine": f"text-to-SQL · {llm.model_name()}"}
            except Exception as e:
                last_err = str(e)[:300]
        fallback_note = f"LLM query failed ({last_err}); used built-in query instead."
    else:
        fallback_note = None

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
    return {"type": "sql", "answer": ans, "title": title, "sql": sql, "table": df, "engine": "built-in query templates"}
