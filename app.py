"""
LeadLens – AI Decision Engine for Sales Data
Run:  streamlit run app.py
"""
import hashlib
import io
import re
from pathlib import Path

import pandas as pd
import streamlit as st

from core import actions, llm
from core.clean import clean
from core.qa import DataStore, answer
from core.scoring import (DEFAULT_WEIGHTS, PRESETS, TARGET_INDUSTRIES, WEIGHT_SPECS, apply_weights,
                          extract_signals, resolve_weights, rubric, suggest_action, weights_changed)

DATA = Path(__file__).parent / "data"
TABLES = ["leads", "deals", "activity", "notes"]
ID_COL = {"leads": "lead_id", "deals": "deal_id", "activity": "activity_id", "notes": "note_id"}

st.set_page_config(page_title="LeadLens · AI Decision Engine", page_icon="🎯", layout="wide")
st.markdown("""
<style>
.block-container {padding-top: 1.6rem;}
.chip {display:inline-block;padding:2px 10px;margin:2px 4px 2px 0;border-radius:12px;background:#eef2ff;color:#3730a3;font-size:0.8rem;}
.pos {color:#15803d;font-weight:600;} .neg {color:#b91c1c;font-weight:600;}
.small {font-size:0.85rem;color:#6b7280;}
</style>""", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Data loading (cached on file contents)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Cleaning data, resolving duplicates and scoring leads…")
def build(key: str, blobs: tuple):
    dfs = [pd.read_csv(io.BytesIO(b)) for b in blobs]
    res = clean(*dfs)
    signals = extract_signals(res.leads, res.deals, res.activity, res.notes, res.ref_date)   # slow part, once
    default_scores = apply_weights(signals)
    store = DataStore(res, default_scores)
    return res, signals, default_scores, store


def ensure_sample():
    if not all((DATA / f"{t}.csv").exists() for t in TABLES):
        import subprocess, sys
        subprocess.run([sys.executable, str(DATA / "generate_data.py")], check=True)


with st.sidebar:
    st.title("🎯 LeadLens")
    st.caption("AI decision engine for sales data")
    src = st.radio("Data source", ["Sample CRM (messy on purpose)", "Upload my CSVs"])
    blobs = None
    if src.startswith("Upload"):
        st.caption("Upload leads.csv, deals.csv, activity.csv, notes.csv (same columns as the sample files).")
        ups = {t: st.file_uploader(f"{t}.csv", type="csv", key=f"up_{t}") for t in TABLES}
        if all(ups.values()):
            blobs = tuple(ups[t].getvalue() for t in TABLES)
        else:
            st.info("Waiting for all 4 files – showing sample data meanwhile.")
    if blobs is None:
        ensure_sample()
        blobs = tuple((DATA / f"{t}.csv").read_bytes() for t in TABLES)
    st.divider()
    reviewer = st.text_input("Reviewer (for audit log)", value="Sales Manager")
    st.divider()
    if llm.available():
        st.success(f"LLM connected: `{llm.model_name()}`")
    else:
        st.warning("No LLM key set – running on built-in rules & templates. Add `LLM_API_KEY` in `.env` for AI answers.")

key = hashlib.md5(b"".join(blobs)).hexdigest()
res, signals, default_scores, store = build(key, blobs)
is_sample = src.startswith("Sample")
ss = st.session_state

# ---------------------------------------------------------------------------
# Scoring weights (edited in the "How scoring works" tab; the whole app re-ranks live)
# ---------------------------------------------------------------------------
EVENT_TYPES = list(DEFAULT_WEIGHTS["event_weights"])


def _set_weights(values: dict):
    w = resolve_weights(values)
    for k in WEIGHT_SPECS:
        ss[f"w_{k}"] = float(w[k])
    for t in EVENT_TYPES:
        ss[f"ev_{t}"] = float(w["event_weights"][t])


if "w_deal_size" not in ss:
    _set_weights({})
weights = resolve_weights({**{k: ss[f"w_{k}"] for k in WEIGHT_SPECS},
                           "event_weights": {t: ss[f"ev_{t}"] for t in EVENT_TYPES}})
custom = weights_changed(weights)
wkey = hashlib.md5(repr(sorted(custom.items())).encode()).hexdigest()[:8] if custom else "default"
scores = default_scores if not custom else apply_weights(signals, weights)   # ≈15 ms

# keep the SQL table used by "Ask your data" in sync with the weights on screen
if getattr(store, "_weights_key", "default") != wkey:
    cols = store.con.execute("SELECT * FROM lead_scores LIMIT 0").df().columns
    store.con.register("_ls", scores[[c for c in cols if c in scores.columns]])
    store.con.execute("CREATE OR REPLACE TABLE lead_scores AS SELECT * FROM _ls")
    store.con.unregister("_ls")
    store._weights_key = wkey

ss.setdefault("explanations", {})
ss.setdefault("decisions", {})
ss.setdefault("chat", [])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def md(text) -> str:
    """Escape $ so Streamlit doesn't render dollar amounts as LaTeX."""
    return str(text).replace("$", "\\$")


def source_rows(table: str, ids: list[str]) -> pd.DataFrame:
    col = ID_COL[table]
    quoted = ",".join(f"'{i}'" for i in ids)
    return store.run_sql(f"SELECT * FROM {table} WHERE {col} IN ({quoted})")


def parse_nl_filter(text: str) -> dict:
    """Very small natural-language filter parser: 'skip anyone contacted in the last 2 weeks, fintech only, over $20k'."""
    t = text.lower()
    f = {}
    m = re.search(r"(?:last|past)\s+(\d+)\s*(day|week|month)", t)
    if m and any(w in t for w in ["exclude", "skip", "not contacted", "without", "remove", "except", "haven't", "havent"]):
        n = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        f["exclude_contacted_days"] = n
    inds = [i for i in sorted(scores["industry"].unique()) if i.lower() in t]
    if inds:
        f["industries"] = inds
    m = re.search(r"(?:over|above|more than|>)\s*\$?\s*(\d+(?:\.\d+)?)\s*(k|m)?", t.replace(",", ""))
    if m:
        f["min_deal"] = int(float(m.group(1)) * {"k": 1e3, "m": 1e6}.get(m.group(2) or "", 1))
    if "stale" in t:
        f["hide_stale"] = True
    m = re.search(r"top\s+(\d+)", t)
    if m:
        f["top_n"] = int(m.group(1))
    return f


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
h = res.health
st.title("LeadLens — who should we contact this week, and why?")
st.caption(f"Dataset date: {res.ref_date.date()} · {h['clean_records']:,} leads · {h['deals']:,} deals · "
           f"{h['activities']:,} activities · {h['notes']:,} call notes")

tab_health, tab_queue, tab_ask, tab_audit, tab_how = st.tabs(
    ["🩺 Data health", "🎯 Action queue", "💬 Ask your data", "🧾 Audit log", "⚙️ How scoring works"])

# ---------------------------------------------------------------------------
# 1. Data health
# ---------------------------------------------------------------------------
with tab_health:
    c = st.columns(5)
    c[0].metric("Raw lead records", f"{h['raw_records']:,}")
    c[1].metric("Duplicates merged", f"{h['duplicates_merged']:,}", help=f"{100*h['duplicates_merged']/h['raw_records']:.1f}% of raw records were duplicates")
    c[2].metric("Clean leads", f"{h['clean_records']:,}")
    c[3].metric(f"Stale / unreachable ({h['stale_pct']}%)", f"{h['stale_records']:,}")
    c[4].metric("Missing / bounced emails", f"{h['missing_email']} / {h['bounced_email']}")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Duplicate records we merged")
        st.caption("Entity resolution: same email, or same company (suffixes ignored) + fuzzy / initial name match. "
                   "Their deals, activity and notes were re-linked to the surviving record.")
        st.dataframe(res.merges.sort_values("match_score"), hide_index=True, width="stretch", height=320)
    with right:
        st.subheader("Why records are stale")
        reasons = res.leads.loc[res.leads["is_stale"], "stale_reason"].str.split("; ").explode()
        reasons = reasons.str.replace(r"no contact in \d+ days", "no contact 180d+", regex=True)
        st.bar_chart(reasons.value_counts(), horizontal=True, height=320)
    with st.expander("Browse cleaned leads"):
        st.dataframe(res.leads.drop(columns=["master_id"], errors="ignore"), hide_index=True, width="stretch")

# ---------------------------------------------------------------------------
# 2. Action queue (decision agent + human approval)
# ---------------------------------------------------------------------------
with tab_queue:
    st.subheader("Ranked action queue")
    nl = st.text_input("Refine in plain English", key="nl",
                       placeholder="e.g. skip anyone contacted in the last 2 weeks, FinTech and Healthcare only, over $20k, top 15")
    parsed = parse_nl_filter(nl) if nl else {}

    fc = st.columns([1, 1, 2, 1, 1])
    top_n = fc[0].number_input("Show top", 5, 50, min(50, parsed.get("top_n", 10)), step=5)
    excl = fc[1].number_input("Skip if contacted in last N days", 0, 90, min(90, parsed.get("exclude_contacted_days", 0)))
    inds = fc[2].multiselect("Industries", sorted(scores["industry"].unique()), default=parsed.get("industries", []))
    min_deal = fc[3].number_input("Min deal ($)", 0, 1_000_000, parsed.get("min_deal", 0), step=5000)
    hide_stale = fc[4].checkbox("Hide stale", value=parsed.get("hide_stale", True))
    if parsed:
        st.markdown("Understood: " + "".join(f"<span class='chip'>{k.replace('_', ' ')}: {v}</span>" for k, v in parsed.items()),
                    unsafe_allow_html=True)

    q = scores.copy()
    if excl:
        q = q[q["days_since_contact"] >= excl]
    if inds:
        q = q[q["industry"].isin(inds)]
    if min_deal:
        q = q[q["top_deal_amount"] >= min_deal]
    if hide_stale:
        q = q[~q["is_stale"]]
    removed = len(scores) - len(q)
    q = q.head(int(top_n))
    st.caption(f"{removed:,} leads filtered out · showing the top {len(q)} of the remaining by transparent score")
    if custom:
        st.markdown("<span class='chip'>⚙️ Custom scoring weights active</span> "
                    "<span class='small'>change or reset them in the <b>How scoring works</b> tab</span>",
                    unsafe_allow_html=True)

    done = ss["decisions"]
    n_app = sum(1 for d in done.values() if d["decision"] == "approved")
    n_rej = sum(1 for d in done.values() if d["decision"] == "rejected")
    st.progress(min(1.0, (n_app + n_rej) / max(1, len(q))), text=f"Reviewed: {n_app} approved · {n_rej} rejected")

    for i, row in enumerate(q.to_dict("records")):
        lid = row["lead_id"]
        # re-explain only when the set of scoring components changes (e.g. a weight switched off),
        # not on every slider nudge – keeps LLM calls (and rate limits) under control
        ekey = (lid, tuple(sorted(c_["component"] for c_ in row["components"] if c_["points"])))
        if ekey not in ss["explanations"]:
            ss["explanations"][ekey] = actions.explain(row)
        action_default = suggest_action(row)
        state = done.get(lid)
        badge = "✅ " if state and state["decision"] == "approved" else "❌ " if state else ""
        deal = f"${row['top_deal_amount']:,.0f} · {row['top_deal_stage']}" if row["top_deal_id"] else "no open deal"
        with st.expander(f"{badge}#{int(row['rank'])} · {row['name']} — {row['company']} · score {row['score']} · {deal}",
                         expanded=(state is None and i == 0)):
            a, b = st.columns([3, 2])
            with a:
                st.markdown(f"**{row['title']}** · {row['industry']} · {row['company_size']:,} employees · "
                            f"last contact {row['days_since_contact']} days ago")
                st.markdown(f"**Why this lead:** {md(ss['explanations'][ekey])}")
                st.markdown(f"**Suggested action:** {action_default}")
                if row["merged_from"]:
                    st.markdown(f"<span class='small'>🔗 Combined from duplicate record(s): {row['merged_from']}</span>",
                                unsafe_allow_html=True)
            with b:
                comp = pd.DataFrame(row["components"])[["component", "points", "fact"]]
                st.dataframe(comp, hide_index=True, width="stretch",
                             column_config={"points": st.column_config.NumberColumn(format="%+.1f")})

            if st.toggle("🔍 Why? Show the source data behind this score", key=f"why_{lid}"):
                for c_ in row["components"]:
                    st.markdown(f"**{c_['component']}** ({c_['points']:+} pts) — {md(c_['fact'])}  \n"
                                f"<span class='small'>source: `{c_['source_table']}` rows {', '.join(c_['source_ids'])}</span>",
                                unsafe_allow_html=True)
                    st.dataframe(source_rows(c_["source_table"], c_["source_ids"]), hide_index=True, width="stretch")

            st.markdown("---")
            if state is None:
                act = st.text_input("Action (edit before approving if needed)", value=action_default, key=f"act_{lid}")
                note = st.text_input("Reviewer note (optional)", key=f"note_{lid}")
                b1, b2, _ = st.columns([1, 1, 4])
                if b1.button("✅ Approve", key=f"ap_{lid}", type="primary"):
                    with st.spinner("Drafting outreach…"):
                        subj, body = actions.draft_email(row, act)
                    entry = actions.log_decision(row, "approved", act, reviewer, note, subj)
                    done[lid] = {**entry, "subject": subj, "body": body}
                    st.rerun()
                if b2.button("❌ Reject", key=f"rj_{lid}"):
                    entry = actions.log_decision(row, "rejected", act, reviewer, note)
                    done[lid] = entry
                    st.rerun()
            else:
                st.markdown(f"**{state['decision'].title()}** by {state['reviewer']} at {state['timestamp']} — action: _{state['action']}_")
                if state["decision"] == "approved":
                    st.text_input("Email subject", value=state["subject"], key=f"subj_{lid}")
                    body = st.text_area("Drafted email (edit freely – nothing is sent automatically)", value=state["body"],
                                        height=200, key=f"body_{lid}")
                    st.download_button("⬇️ Download draft", f"To: {row['email']}\nSubject: {state['subject']}\n\n{body}",
                                       file_name=f"email_{lid}.txt", key=f"dl_{lid}")
                if st.button("↩️ Undo decision", key=f"undo_{lid}"):
                    actions.log_decision(row, "undone", state["action"], reviewer)
                    del done[lid]
                    st.rerun()

# ---------------------------------------------------------------------------
# 3. Ask your data (analytics agent + RAG)
# ---------------------------------------------------------------------------
with tab_ask:
    st.subheader("Ask a question about your pipeline")
    examples = ["Which deals over $50k are stuck?", "Which Negotiation deals close this month?",
                "Show pipeline by industry", "Who complained about pricing?", "Which customers mentioned a competitor?",
                "Top 10 leads to call"]
    ex_cols = st.columns(3)
    clicked = None
    for i, ex in enumerate(examples):
        if ex_cols[i % 3].button(ex, key=f"ex_{i}", width="stretch"):
            clicked = ex
    typed = st.chat_input("Ask in plain English…")
    question = typed or clicked
    if question:
        with st.spinner("Thinking…"):
            try:
                out = answer(store, question)
            except Exception as e:
                out = {"type": "error", "answer": f"Sorry, I couldn't answer that: {e}", "table": pd.DataFrame(), "sql": None, "engine": "-"}
        ss["chat"].insert(0, {"q": question, **out})

    for turn in ss["chat"][:8]:
        with st.chat_message("user"):
            st.write(turn["q"])
        with st.chat_message("assistant"):
            st.markdown(md(turn["answer"]))
            st.caption(f"engine: {turn['engine']}")
            if turn.get("sql"):
                with st.expander("🔍 SQL that produced this answer"):
                    st.code(turn["sql"], language="sql")
            if isinstance(turn.get("table"), pd.DataFrame) and len(turn["table"]):
                label = "📄 Source call notes" if turn["type"] == "notes" else "📄 Result rows"
                with st.expander(label, expanded=True):
                    st.dataframe(turn["table"], hide_index=True, width="stretch")

# ---------------------------------------------------------------------------
# 4. Audit log
# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Every human decision, with the evidence it was based on")
    log = actions.read_audit()
    if log:
        df = pd.DataFrame(log).iloc[::-1]
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("⬇️ Export audit log (CSV)", df.to_csv(index=False), "audit_log.csv")
    else:
        st.info("No decisions yet. Approve or reject leads in the Action queue.")

# ---------------------------------------------------------------------------
# 5. How scoring works
# ---------------------------------------------------------------------------
with tab_how:
    st.subheader("Transparent scoring rubric")
    st.markdown("The **score is computed by explicit rules**, never by the language model. The AI only writes the explanation "
                "and the email draft, using the facts below. Every point links back to specific rows. "
                "**Adjust the weights below and every tab re-ranks instantly** — the evidence behind each point never changes, "
                "only how much it counts.")

    pc = st.columns([2, 1, 3])
    preset = pc[0].selectbox("Start from a preset", list(PRESETS), key="preset")
    pc[1].button("Apply preset", on_click=_set_weights, args=(PRESETS[preset],), width="stretch")
    pc[1].button("↺ Reset to default", on_click=_set_weights, args=({},), width="stretch")
    if custom:
        pc[2].markdown("**Changed from default:** " + "".join(
            f"<span class='chip'>{WEIGHT_SPECS[k][0] if k in WEIGHT_SPECS else k}: "
            f"{v if not isinstance(v, dict) else ', '.join(f'{a}={b:g}' for a, b in v.items())}</span>"
            for k, v in custom.items()), unsafe_allow_html=True)
    else:
        pc[2].caption("Using the default weights.")

    wl, wr = st.columns(2)
    for i_, (k, (label, lo, hi, help_)) in enumerate(WEIGHT_SPECS.items()):
        (wl if i_ % 2 == 0 else wr).slider(label, float(lo), float(hi), step=1.0, key=f"w_{k}", help=help_)
    with st.expander("Engagement event weights (how much each kind of activity counts)"):
        ec = st.columns(3)
        for i_, t in enumerate(EVENT_TYPES):
            ec[i_ % 3].number_input(t.replace("_", " "), 0.0, 20.0, step=1.0, key=f"ev_{t}")
        st.caption(f"Full engagement points are earned at {weights['engagement_saturation']:g} weighted events in the last 30 days.")

    st.markdown("#### Rubric with the current weights")
    st.table(pd.DataFrame(rubric(weights), columns=["Component", "Rule"]))
    st.markdown(f"Target industries: {', '.join(sorted(TARGET_INDUSTRIES))}")

    st.markdown("#### Live effect on the ranking")
    prev = default_scores.set_index("lead_id")
    top = scores.head(15)[["rank", "lead_id", "name", "company", "score", "top_deal_amount", "is_stale"]].copy()
    top["default rank"] = top["lead_id"].map(prev["rank"])
    top["move"] = (top["default rank"] - top["rank"]).map(lambda d: "—" if d == 0 else f"▲ {d}" if d > 0 else f"▼ {-d}")
    top["default score"] = top["lead_id"].map(prev["score"])
    st.dataframe(top, hide_index=True, width="stretch",
                 column_config={"top_deal_amount": st.column_config.NumberColumn("top deal ($)", format="%,.0f")})
    if is_sample:
        try:
            from eval.eval_ranking import load_truth, precision_at_k, truly_hot_ids
            truth = load_truth(leads=res.leads)
            if truth is None:
                st.caption("Ranking evaluation: no planted-lead ground truth for this data "
                           "(run `python data/plant_hot_leads.py`).")
            else:
                true_hot = truly_hot_ids(default_scores, truth)   # checklist facts don't depend on weights
                m = st.columns(3)
                p_now = precision_at_k(scores, true_hot, 20)
                p_def = precision_at_k(default_scores, true_hot, 20)
                m[0].metric("Truly hot leads in top 20 (precision@20)", f"{p_now:.0%}",
                            delta=f"{(p_now - p_def) * 100:+.0f} pts vs default" if custom else None,
                            help="Planted hot leads + organic leads passing the same hot checklist. See eval/eval_ranking.py")
                m[1].metric("Strict: planted hot leads only", f"{precision_at_k(scores, truth, 20):.0%}")
                m[2].metric("Decoys in top 20", int(scores.head(20)["lead_id"].isin(truth[truth['label'] == 'decoy']['lead_id']).sum()),
                            help="Planted leads that look hot on ONE signal only (big stale deal, lost to a competitor, …)")
        except Exception as e:  # evaluation is optional – never break the demo
            st.caption(f"Ranking evaluation unavailable: {e}")

    st.markdown("**Pipeline:** raw CSVs → clean & de-duplicate → DuckDB → (a) rule-based scoring → ranked queue → human approval → audit log; "
                "(b) questions → text-to-SQL or notes retrieval → answer with SQL / note-id citations.")
    st.bar_chart(scores["score"].round(-1).value_counts().sort_index(), height=220)
