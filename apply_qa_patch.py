"""Applies the Member-2 patch to core/qa.py. Backup -> core/qa.py.bak
Run from the repo root:  python apply_qa_patch.py
"""
import re, shutil, sys
from pathlib import Path

P = Path(__file__).resolve().parent / "core" / "qa.py"
src = P.read_text(encoding="utf-8")
if "def known_ids" in src:
    sys.exit("qa.py is already patched.")
shutil.copy(P, P.with_suffix(".py.bak"))
src = src.replace("\r\n", "\n")

DATASTORE = r'''        # notes retrieval index: embeddings + Chroma (auto-falls back to TF-IDF)
        self.notes = notes.merge(leads[["lead_id", "name", "company"]], on="lead_id", how="left")
        self.index = NotesIndex(self.notes)
        self.rag_engine = self.index.engine
        self._known_ids = None
'''

METHODS = r'''    def known_ids(self) -> set[str]:
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
'''

ANSWER = r'''# ---------------------------------------------------------------------------
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
            ans, rep = None, no_cite
            if summarize:
                try:
                    ans = llm.chat(
                        "You are a precise sales analyst. Answer the question using ONLY numbers present in the SQL result. "
                        "Mention ids (lead_id / deal_id) for specific records. 2-5 sentences or short bullets. Never invent data.",
                        f"Question: {question}\nSQL: {sql}\nResult ({len(df)} rows, first 25 shown):\n{_fmt(df)}")
                    ans, rep = check_citations(ans, store.known_ids())
                except Exception:
                    ans = None
            if ans is None:
                total = df.attrs.get("total_rows", len(df))
                ans = f"**{title}** — **{total:,}** row(s). See the table and SQL below."
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
'''

src = re.sub(r"^from sklearn\.[^\n]*\n", "", src, flags=re.M)
src, n1 = re.subn(r"^from \. import llm[ \t]*$", "from . import llm\nfrom .citations import check_citations\nfrom .rag import NotesIndex", src, count=1, flags=re.M)
src, n2 = re.subn(r"^[ \t]*# notes retrieval index.*?self\.mat = self\.vec\.fit_transform\(docs\)[^\n]*\n", lambda m: DATASTORE, src, count=1, flags=re.M | re.S)
src, n3 = re.subn(r"^[ \t]*# -+ Notes RAG -+[^\n]*\n[ \t]*def search_notes.*?^[ \t]*return res\[\[[^\n]*\n", lambda m: METHODS, src, count=1, flags=re.M | re.S)
if not n3:
    src, n3 = re.subn(r"^[ \t]*def search_notes.*?^[ \t]*return res\[\[[^\n]*\n", lambda m: METHODS, src, count=1, flags=re.M | re.S)
m = re.search(r"^# -+[ \t]*\n# Main entry point[^\n]*\n# -+[ \t]*\n", src, flags=re.M) or re.search(r"^def answer\(", src, flags=re.M)
if not (n1 and n2 and n3 and m):
    sys.exit(f"Patch failed (imports={n1}, index={n2}, search_notes={n3}, answer={bool(m)}). qa.py NOT changed.")
src = src[:m.start()] + ANSWER
P.write_text(src, encoding="utf-8")
print("Patched core/qa.py  (backup: core/qa.py.bak)")
