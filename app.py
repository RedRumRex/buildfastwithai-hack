"""
LeadLens – AI Decision Engine for Sales Data
Run:  streamlit run app.py
"""
import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pandas as pd
import streamlit as st

from core import actions, llm
from core import filters as nl_filters
from core.clean import clean
from core.ingest import validate_tables
from core.qa import DataStore, answer
from core.scoring import (DEFAULT_WEIGHTS, OPEN_STAGES, PRESETS, TARGET_INDUSTRIES, WEIGHT_SPECS, apply_weights,
                          extract_signals, resolve_weights, rubric, suggest_action, weights_changed)

DATA = Path(__file__).parent / "data"
TABLES = ["leads", "deals", "activity", "notes"]
ID_COL = {"leads": "lead_id", "deals": "deal_id", "activity": "activity_id", "notes": "note_id"}

st.set_page_config(page_title="LeadLens · AI Decision Engine", page_icon="🎯", layout="wide")


def _secrets_to_env():
    """On Streamlit Cloud the LLM key lives in the app's Secrets, not in .env – copy it into the environment
    so core/llm.py finds it the same way locally and in the cloud. Silently does nothing without secrets."""
    try:
        for k in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
            if not os.getenv(k) and k in st.secrets:
                os.environ[k] = str(st.secrets[k])
    except Exception:
        pass


_secrets_to_env()
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
@st.cache_resource
def db_lock() -> threading.RLock:
    """One DuckDB connection is shared by every visitor of the deployed app (st.cache_resource), and a DuckDB
    connection must not be used from two threads at once – so every query goes through this lock."""
    return threading.RLock()


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
    blobs, upload_errors, upload_notes = None, [], []
    if src.startswith("Upload"):
        st.caption("Upload leads.csv, deals.csv, activity.csv, notes.csv (same columns as the sample files). "
                   "Header variants such as `Email`, `E-mail` or `Company Name` are recognised.")
        ups = {t: st.file_uploader(f"{t}.csv", type="csv", key=f"up_{t}") for t in TABLES}
        if all(ups.values()):
            check = validate_tables({t: ups[t].getvalue() for t in TABLES})
            upload_notes = [f"{t}.csv: read '{a}' as '{b}'" for t, m in check.renamed.items() for a, b in m.items()]
            upload_notes += check.warnings
            if check.ok:
                blobs = tuple(check.tables[t].to_csv(index=False).encode() for t in TABLES)
                st.success("All 4 files passed the checks.")
            else:
                upload_errors = check.errors
                st.error("Your files can't be used yet (see the details on the right). Showing sample data meanwhile.")
            if upload_notes:
                with st.expander(f"{len(upload_notes)} note(s) about your files"):
                    st.markdown("\n".join(f"- {n}" for n in upload_notes))
        else:
            st.info("Waiting for all 4 files – showing sample data meanwhile.")
    uploaded = blobs is not None
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

if upload_errors:
    st.error("**Your upload can't be used yet – fix these and upload again:**\n\n" +
             "\n".join(f"- {e}" for e in upload_errors) + "\n\nThe app is showing the sample data meanwhile.")
key = hashlib.md5(b"".join(blobs)).hexdigest()
try:
    res, signals, default_scores, store = build(key, blobs)
except Exception as e:   # passed the column checks but something deeper failed: never crash the demo
    if not uploaded:
        raise
    st.error(f"Your files passed the checks but could not be processed ({type(e).__name__}: {str(e)[:200]}). "
             "Showing the sample data instead.")
    uploaded = False
    ensure_sample()
    blobs = tuple((DATA / f"{t}.csv").read_bytes() for t in TABLES)
    key = hashlib.md5(b"".join(blobs)).hexdigest()
    res, signals, default_scores, store = build(key, blobs)
is_sample = not uploaded
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



def sync_lead_scores():
    """Make the SQL table used by "Ask your data" match THIS visitor's weights. Call only while holding
    db_lock(), right before querying – other visitors may be using different weights."""
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
    quoted = ",".join("'" + str(i).replace("'", "''") + "'" for i in ids)
    with db_lock():
        return store.run_sql(f"SELECT * FROM {table} WHERE {col} IN ({quoted})")


@st.cache_data(show_spinner=False, max_entries=256)
def parse_filter(text: str, industries: tuple, stages: tuple, use_llm: bool) -> tuple[dict, str]:
    """Plain-English filter -> structured filters. LLM when available (validated), regex rules as fallback.
    Cached so a Streamlit rerun never re-calls the LLM for the same sentence."""
    return nl_filters.parse(text, list(industries), list(stages), use_llm=use_llm)


WEIGHTS_TAG = json.dumps(custom, sort_keys=True) if custom else "default"   # stored in the audit log


def eml(to: str, subject: str, body: str) -> str:
    """An .eml file opens as an editable, unsent draft in Mail / Outlook / Thunderbird."""
    return (f"To: {to}\nSubject: {subject}\nX-Unsent: 1\nMIME-Version: 1.0\n"
            f"Content-Type: text/plain; charset=utf-8\n\n{body}\n")


def _approve(row: dict, reviewer: str, weights_tag: str, action: str | None = None, note: str | None = None,
             details: str = ""):
    lid = row["lead_id"]
    act = action if action is not None else ss.get(f"act_{lid}") or suggest_action(row)
    note = note if note is not None else ss.get(f"note_{lid}", "")
    subj, body = actions.draft_email(row, act)
    entry = actions.log_decision(row, "approved", act, reviewer, note, subj, body, details, weights_tag)
    ss["decisions"][lid] = {**entry, "subject": subj, "body": body, "row": row}


def _reject(row: dict, reviewer: str, weights_tag: str, action: str | None = None, note: str | None = None,
            details: str = ""):
    lid = row["lead_id"]
    act = action if action is not None else ss.get(f"act_{lid}") or suggest_action(row)
    note = note if note is not None else ss.get(f"note_{lid}", "")
    entry = actions.log_decision(row, "rejected", act, reviewer, note, details=details, weights=weights_tag)
    ss["decisions"][lid] = {**entry, "row": row}


def _bulk(kind: str, rows: list[dict], reviewer: str, weights_tag: str):
    sel = set(ss.get("bulk_sel", []))
    todo = [r for r in rows if r["lead_id"] in sel and r["lead_id"] not in ss["decisions"]]
    note = ss.get("bulk_note", "")
    for r in todo:
        fn = _approve if kind == "approve" else _reject
        fn(r, reviewer, weights_tag, action=suggest_action(r), note=note, details=f"bulk {kind} ({len(todo)} leads)")
    ss["bulk_sel"] = []
    ss["bulk_msg"] = f"{'Approved' if kind == 'approve' else 'Rejected'} {len(todo)} lead(s) — each one is logged separately in the audit log."


def _save_edit(lid: str, reviewer: str, weights_tag: str):
    d = ss["decisions"][lid]
    subj, body = ss.get(f"subj_{lid}", d["subject"]), ss.get(f"body_{lid}", d["body"])
    changes = []
    if subj != d["subject"]:
        changes.append("subject changed")
    if body != d["body"]:
        delta = len(body) - len(d["body"])
        changes.append(f"body changed ({delta:+d} chars)")
    if not changes:
        return
    actions.log_decision(d["row"], "edited", d["action"], reviewer, d.get("reviewer_note", ""), subj, body,
                         "; ".join(changes), weights_tag)
    d["subject"], d["body"] = subj, body
    d["edits"] = d.get("edits", 0) + 1


def _undo(lid: str, reviewer: str, weights_tag: str):
    d = ss["decisions"].pop(lid, None)
    if d is None:
        return
    actions.log_decision(d["row"], "undone", d["action"], reviewer, subject=d.get("subject", ""),
                         details=f"undid '{d['decision']}'", weights=weights_tag)
    for k in (f"subj_{lid}", f"body_{lid}"):
        ss.pop(k, None)


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
    c = st.columns(6)
    c[0].metric("Raw lead records", f"{h['raw_records']:,}")
    c[1].metric("Duplicates merged", f"{h['duplicates_merged']:,}", help=f"{100*h['duplicates_merged']/h['raw_records']:.1f}% of raw records were duplicates")
    c[2].metric("Possible duplicates", f"{h.get('possible_duplicates', 0):,}", help="Similar records NOT merged automatically – a person decides (list below)")
    c[3].metric("Clean leads", f"{h['clean_records']:,}")
    c[4].metric(f"Stale / unreachable ({h['stale_pct']}%)", f"{h['stale_records']:,}")
    c[5].metric("Missing / bounced / invalid emails", f"{h['missing_email']} / {h['bounced_email']} / {h.get('invalid_email', 0)}",
                help="Missing = raw records without an email; bounced and invalid = clean leads")

    ba, fi = st.columns([2, 3])
    with ba:
        st.subheader("Before → after cleaning")
        t = pd.DataFrame(h.get("before_after", []))
        if len(t):
            t["change"] = (t["after"] - t["before"]).map(lambda d: "" if pd.isna(d) else f"{d:+,.0f}")
            st.dataframe(t, hide_index=True, width="stretch",
                         column_config={"before": st.column_config.NumberColumn(format="%,d"),
                                        "after": st.column_config.NumberColumn(format="%,d")})
        conf = h.get("conflicts_resolved", {})
        if conf:
            st.caption(f"Merged records that disagreed: {conf.get('title', 0)} on job title, {conf.get('phone', 0)} on phone number. "
                       "The oldest record's value is kept; a well-formed email beats a mistyped one, and missing phones are "
                       "filled in from the duplicate.")
    with fi:
        st.subheader("Field-level issues")
        t = pd.DataFrame(h.get("field_issues", []))
        if len(t):
            t = t[(t["before"] > 0) | (t["after"] > 0)]
            t.insert(5, "fixed", t["before"] - t["after"])
            st.dataframe(t, hide_index=True, width="stretch",
                         column_config={"example_ids": st.column_config.TextColumn("example IDs", help="Look these up in Browse cleaned leads below")})
            st.caption("Before = raw records, after = cleaned leads. Merging duplicates fixes many issues (a duplicate with a "
                       "blank or mistyped email is folded into a record with a good one); the rest need fixing at the source.")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Duplicate records we merged")
        rules = res.merges["rule"].value_counts()
        st.caption("Entity resolution: " + " · ".join(f"{r} ({n})" for r, n in rules.items()) +
                   ". Their deals, activity and notes were re-linked to the surviving record.")
        st.dataframe(res.merges.sort_values("match_score"), hide_index=True, width="stretch", height=320)
    with right:
        st.subheader("Why records are stale")
        reasons = res.leads.loc[res.leads["is_stale"], "stale_reason"].str.split("; ").explode()
        reasons = reasons.str.replace(r"no contact in \d+ days", "no contact 180d+", regex=True)
        st.bar_chart(reasons.value_counts(), horizontal=True, height=320)

    review = getattr(res, "review", None)
    if review is not None and len(review):
        st.subheader(f"Possible duplicates – needs review ({len(review)})")
        st.caption("Similar enough to look like the same person, but without enough evidence to merge automatically "
                   "(for example a similar name at a different company, with no shared email or phone). "
                   "They are kept as separate leads; check them in the CRM.")
        st.dataframe(review, hide_index=True, width="stretch")
    with st.expander("Browse cleaned leads"):
        st.dataframe(res.leads.drop(columns=["master_id"], errors="ignore"), hide_index=True, width="stretch")

# ---------------------------------------------------------------------------
# 2. Action queue (decision agent + human approval)
# ---------------------------------------------------------------------------
with tab_queue:
    st.subheader("Ranked action queue")
    all_inds = tuple(sorted(scores["industry"].dropna().unique()))
    nl = st.text_input("Refine in plain English", key="nl",
                       placeholder="e.g. skip anyone contacted in the last 2 weeks, FinTech and Healthcare only, "
                                   "late-stage deals over $20k, top 15")
    parsed, engine = parse_filter(nl, all_inds, tuple(OPEN_STAGES), llm.available()) if nl.strip() else ({}, None)

    fc = st.columns([1, 1, 2, 2, 1, 1])
    top_n = fc[0].number_input("Show top", 1, 50, int(min(50, max(1, parsed.get("top_n", 10)))), step=5)
    excl = fc[1].number_input("Skip if contacted in last N days", 0, 365, int(min(365, parsed.get("exclude_contacted_days", 0))))
    inds = fc[2].multiselect("Industries", all_inds, default=[i for i in parsed.get("industries", []) if i in all_inds])
    stages = fc[3].multiselect("Deal stage (top open deal)", OPEN_STAGES, default=parsed.get("stages", []))
    min_deal = fc[4].number_input("Min deal ($)", 0, 100_000_000, int(min(100_000_000, parsed.get("min_deal", 0))), step=5000)
    hide_stale = fc[5].checkbox("Hide stale", value=parsed.get("hide_stale", True))
    if nl.strip():
        if parsed:
            who = "🤖 AI parser" if engine == "llm" else "📏 rule-based parser"
            st.markdown(f"<span class='small'>Understood by the {who}:</span> " + "".join(
                f"<span class='chip'>{k.replace('_', ' ')}: {', '.join(v) if isinstance(v, list) else f'{v:,}' if isinstance(v, int) and not isinstance(v, bool) else v}</span>"
                for k, v in parsed.items()), unsafe_allow_html=True)
        else:
            st.caption("Couldn't turn that into a filter — try e.g. “FinTech only, over $20k, skip anyone contacted in the last 2 weeks”.")

    q = scores.copy()
    if excl:
        q = q[~(q["days_since_contact"] < excl)]   # keeps leads with an unknown contact date (not "recently contacted")
    if inds:
        q = q[q["industry"].isin(inds)]
    if stages:
        q = q[q["top_deal_stage"].isin(stages)]
    if min_deal:
        q = q[q["top_deal_amount"] >= min_deal]
    if hide_stale:
        q = q[~q["is_stale"]]
    removed = len(scores) - len(q)
    q = q.head(int(top_n))
    rows = q.to_dict("records")
    st.caption(f"{removed:,} leads filtered out · showing the top {len(q)} of the remaining by transparent score")
    if custom:
        st.markdown("<span class='chip'>⚙️ Custom scoring weights active</span> "
                    "<span class='small'>change or reset them in the <b>How scoring works</b> tab</span>",
                    unsafe_allow_html=True)

    done = ss["decisions"]
    shown = [r["lead_id"] for r in rows]
    n_app = sum(1 for l in shown if l in done and done[l]["decision"] == "approved")
    n_rej = sum(1 for l in shown if l in done and done[l]["decision"] == "rejected")
    st.progress((n_app + n_rej) / max(1, len(rows)), text=f"Reviewed {n_app + n_rej} of {len(rows)} shown: {n_app} approved · {n_rej} rejected")

    # ---- bulk review -----------------------------------------------------------
    pending = [r for r in rows if r["lead_id"] not in done]
    labels = {r["lead_id"]: f"#{int(r['rank'])} · {r['name']} — {r['company']} · score {r['score']}" for r in rows}
    ss["bulk_sel"] = [x for x in ss.get("bulk_sel", []) if x in {r["lead_id"] for r in pending}]
    with st.container(border=True):
        st.markdown("**Bulk review** <span class='small'>— each lead gets the suggested action; every decision is logged individually</span>",
                    unsafe_allow_html=True)
        bc = st.columns([5, 1, 1])
        bc[0].multiselect("Select pending leads", [r["lead_id"] for r in pending], format_func=labels.get,
                          key="bulk_sel", label_visibility="collapsed", placeholder="Select pending leads…")
        bc[1].button("Select all", width="stretch", disabled=not pending,
                     on_click=lambda ids=[r["lead_id"] for r in pending]: ss.update(bulk_sel=ids))
        bc[2].button("Clear", width="stretch", on_click=lambda: ss.update(bulk_sel=[]))
        n_sel = len(ss["bulk_sel"])
        bb = st.columns([3, 1, 1])
        bb[0].text_input("Note for the bulk decision (optional)", key="bulk_note", label_visibility="collapsed",
                         placeholder="Note for the bulk decision (optional)")
        bb[1].button(f"✅ Approve {n_sel}", type="primary", width="stretch", disabled=not n_sel, key="bulk_approve",
                     on_click=_bulk, args=("approve", rows, reviewer, WEIGHTS_TAG))
        bb[2].button(f"❌ Reject {n_sel}", width="stretch", disabled=not n_sel, key="bulk_reject",
                     on_click=_bulk, args=("reject", rows, reviewer, WEIGHTS_TAG))
    if ss.get("bulk_msg"):
        st.success(ss.pop("bulk_msg"))

    approved = [d for d in done.values() if d["decision"] == "approved"]
    if approved:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for d in approved:
                z.writestr(f"email_{d['lead_id']}.eml", eml(d["row"]["email"], d["subject"], d["body"]))
        st.download_button(f"⬇️ Download all {len(approved)} approved email drafts (.zip of .eml)", buf.getvalue(),
                           file_name="leadlens_email_drafts.zip", mime="application/zip", key="dl_all")

    # ---- one card per lead -------------------------------------------------------
    for i, row in enumerate(rows):
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
                            + (f"last contact {int(row['days_since_contact'])} days ago" if pd.notna(row['days_since_contact']) else "no contact date on record"))
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
                st.text_input("Action (edit before approving if needed)", value=action_default, key=f"act_{lid}")
                st.text_input("Reviewer note (optional)", key=f"note_{lid}")
                b1, b2, _ = st.columns([1, 1, 4])
                b1.button("✅ Approve", key=f"ap_{lid}", type="primary", on_click=_approve, args=(row, reviewer, WEIGHTS_TAG))
                b2.button("❌ Reject", key=f"rj_{lid}", on_click=_reject, args=(row, reviewer, WEIGHTS_TAG))
            else:
                bulk = " (bulk)" if str(state.get("details", "")).startswith("bulk") else ""
                st.markdown(f"**{state['decision'].title()}{bulk}** by {state['reviewer']} at {state['timestamp']} — action: _{md(state['action'])}_")
                if state["decision"] == "approved":
                    subj = st.text_input("Email subject", value=state["subject"], key=f"subj_{lid}")
                    body = st.text_area("Drafted email (edit freely – nothing is sent automatically)", value=state["body"],
                                        height=200, key=f"body_{lid}")
                    dirty = subj != state["subject"] or body != state["body"]
                    e1, e2, e3 = st.columns([1, 1, 3])
                    e1.button("💾 Save edits", key=f"save_{lid}", disabled=not dirty, type="primary" if dirty else "secondary",
                              on_click=_save_edit, args=(lid, reviewer, WEIGHTS_TAG))
                    e2.download_button("⬇️ Download .eml", eml(row["email"], state["subject"], state["body"]),
                                       file_name=f"email_{lid}.eml", mime="message/rfc822", key=f"dl_{lid}",
                                       help="Opens as an unsent draft in Mail / Outlook. Downloads the last SAVED version.")
                    if dirty:
                        e3.caption("✏️ Unsaved edits — save them to log the change and include them in the download.")
                    elif state.get("edits"):
                        e3.caption(f"Edited {state['edits']}× — every edit is in the audit log.")
                st.button("↩️ Undo decision", key=f"undo_{lid}", on_click=_undo, args=(lid, reviewer, WEIGHTS_TAG))

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
                with db_lock():          # shared DB: one query at a time, with this visitor's weights
                    sync_lead_scores()
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
    st.caption("Approvals, rejections, email edits and undos are appended to the log as they happen — nothing is ever "
               "overwritten. Each entry keeps the evidence row ids behind the score and the weight profile that ranked it.")
    log = actions.read_audit()
    if not log:
        st.info("No decisions yet. Approve or reject leads in the Action queue.")
    else:
        df = pd.DataFrame(log).iloc[::-1].reset_index(drop=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df["score"] = pd.to_numeric(df["score"], errors="coerce")
        counts = df["decision"].value_counts()
        mc = st.columns(5)
        mc[0].metric("Events", len(df))
        for col_, (k, lbl) in zip(mc[1:], [("approved", "✅ Approved"), ("rejected", "❌ Rejected"),
                                           ("edited", "✏️ Edited"), ("undone", "↩️ Undone")]):
            col_.metric(lbl, int(counts.get(k, 0)))

        fa = st.columns([2, 2, 2, 2])
        dec_opts = [d for d in ["approved", "rejected", "edited", "undone"] if d in counts] + \
                   sorted(set(counts.index) - {"approved", "rejected", "edited", "undone"})
        f_dec = fa[0].multiselect("Decision", dec_opts, key="aud_dec", placeholder="All decisions")
        f_rev = fa[1].multiselect("Reviewer", sorted(df["reviewer"].dropna().unique()), key="aud_rev", placeholder="All reviewers")
        f_txt = fa[2].text_input("Search lead / company / text", key="aud_q", placeholder="e.g. L02156 or Slater")
        dates = df["timestamp"].dropna().dt.date
        f_dates = fa[3].date_input("Date range", value=(dates.min(), dates.max()), key="aud_dates") if len(dates) else None
        f_bulk = st.checkbox("Only bulk decisions", key="aud_bulk")

        v = df
        if f_dec:
            v = v[v["decision"].isin(f_dec)]
        if f_rev:
            v = v[v["reviewer"].isin(f_rev)]
        if f_txt.strip():
            needle = f_txt.strip().lower()
            hay = v[["lead_id", "name", "company", "action", "reviewer_note", "email_subject", "details"]].fillna("").astype(str) \
                .agg(" ".join, axis=1).str.lower()
            v = v[hay.str.contains(needle, regex=False)]
        if isinstance(f_dates, (tuple, list)) and len(f_dates) == 2:
            d = v["timestamp"].dt.date
            v = v[(d >= f_dates[0]) & (d <= f_dates[1])]
        if f_bulk:
            v = v[v["details"].fillna("").str.startswith("bulk")]

        st.caption(f"Showing {len(v):,} of {len(df):,} events")
        cols = ["timestamp", "decision", "reviewer", "lead_id", "name", "company", "score", "action", "email_subject",
                "reviewer_note", "details", "weights", "evidence_ids", "email_body"]
        st.dataframe(v[[c for c in cols if c in v.columns]], hide_index=True, width="stretch",
                     column_config={"timestamp": st.column_config.DatetimeColumn(format="YYYY-MM-DD HH:mm:ss"),
                                    "score": st.column_config.NumberColumn(format="%.1f"),
                                    "email_body": st.column_config.TextColumn(width="large")})
        ex = st.columns([1, 1, 3])
        out = lambda d_: d_.assign(timestamp=d_["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S")).to_csv(index=False)
        ex[0].download_button(f"⬇️ Export filtered ({len(v):,}) CSV", out(v), "audit_log_filtered.csv", mime="text/csv",
                              key="aud_dl_f", disabled=v.empty)
        ex[1].download_button(f"⬇️ Export full log ({len(df):,}) CSV", out(df), "audit_log.csv", mime="text/csv", key="aud_dl_all")

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
