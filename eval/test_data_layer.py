"""
Independent tests for the data layer (Member 1: core/clean.py, core/ingest.py, data/generate_data.py),
written against plan.md – NOT against Member 1's own evaluation script, so a bug can't hide in both.

    python eval/test_data_layer.py            # ~30-60 s; exit code 0 = no FAIL

Result levels
  PASS – behaves as the plan / interfaces require
  FAIL – breaks a plan requirement, an agreed interface, or crashes the pipeline  -> must fix
  WARN – works, but a judgement call the team should decide on (not a plan requirement)

Groups
  A  Interface contract   – what scoring.py, qa.py and app.py rely on (plan.md section 3)
  B  Data integrity       – nothing lost, nothing orphaned, numbers add up
  C  Dedupe vs truth      – recall / false merges computed here, independently
  D  Determinism          – same input (in any row order) -> same result
  E  Entity-resolution edge cases – tiny hand-made tables with a known right answer
  F  Upload validation    – column variants, clear errors, no crashes
  G  End to end           – upload -> clean -> score -> SQL store, including awkward uploads
  H  Scale                – a 5,000-lead CRM still cleans quickly enough for the hosted app
"""
import io
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.clean import CleanResult, clean          # noqa: E402
from core.ingest import validate_tables            # noqa: E402

DATA = ROOT / "data"
TABLES = ["leads", "deals", "activity", "notes"]
results = []


def check(group, name, level_on_fail="FAIL"):
    """Decorator: run a test, record PASS / FAIL / WARN. A test returns a short detail string or raises."""
    def deco(fn):
        t = time.time()
        try:
            detail = fn() or ""
            status = "PASS"
        except AssertionError as e:
            status, detail = level_on_fail, str(e) or "assertion failed"
        except Exception as e:
            status, detail = "FAIL", f"crashed: {type(e).__name__}: {e}"
            if os.getenv("VERBOSE"):
                traceback.print_exc()
        results.append((group, name, status, detail))
        print(f"{status:<4}  {group}  {name:<62} {time.time() - t:5.1f}s  {str(detail)[:150]}")
        return fn
    return deco


# ---------------------------------------------------------------------------------------------
# shared fixtures
# ---------------------------------------------------------------------------------------------
RAW = {t: pd.read_csv(DATA / f"{t}.csv") for t in TABLES}
TRUTH = pd.read_csv(DATA / "truth_duplicates.csv")
T0 = time.time()
RES = clean(*[RAW[t] for t in TABLES])
CLEAN_SECS = time.time() - T0
L = RES.leads

LEAD_COLS = {"lead_id", "name", "company", "email", "phone", "title", "industry", "company_size", "country", "source",
             "created_at", "last_contact_date", "email_status", "merged_from", "n_sources",
             "days_since_contact", "is_stale", "stale_reason"}
CHILD_COLS = {"deals": ["deal_id", "lead_id", "deal_name", "amount_usd", "stage", "expected_close_date", "last_stage_change"],
              "activity": ["activity_id", "lead_id", "type", "activity_date"],
              "notes": ["note_id", "lead_id", "note_date", "author", "text"]}
APP_HEALTH_KEYS = {"raw_records", "clean_records", "duplicates_merged", "missing_email", "bounced_email",
                   "stale_records", "stale_pct", "deals", "activities", "notes"}


def groups_of(res: CleanResult) -> dict:
    """surviving lead_id -> set of all raw lead_ids folded into it (itself included)."""
    g = {}
    for lid, mf in zip(res.leads["lead_id"], res.leads["merged_from"].fillna("")):
        g[lid] = {lid} | {x.strip() for x in str(mf).split(",") if x.strip()}
    return g


def lead(lid, name, company, email="", phone="", created="2025-01-01", last="2026-09-01", industry="FinTech",
         country="India", size=250, title="CTO", status="valid"):
    return {"lead_id": lid, "name": name, "company": company, "email": email, "phone": phone, "title": title,
            "industry": industry, "company_size": size, "country": country, "source": "Website",
            "created_at": created, "last_contact_date": last, "email_status": status}


def tables(leads, deals=None, activity=None, notes=None):
    """Small hand-made CRM. One activity row by default so the dataset has a reference date."""
    lids = [l["lead_id"] for l in leads]
    activity = activity if activity is not None else [
        {"activity_id": "A1", "lead_id": lids[0], "type": "email_open", "activity_date": "2026-09-10"}]
    notes = notes if notes is not None else [
        {"note_id": "N1", "lead_id": lids[0], "note_date": "2026-09-05", "author": "Rep", "text": "Intro call went fine."}]
    deals = deals if deals is not None else []
    return (pd.DataFrame(leads), pd.DataFrame(deals, columns=CHILD_COLS["deals"]),
            pd.DataFrame(activity, columns=CHILD_COLS["activity"]), pd.DataFrame(notes, columns=CHILD_COLS["notes"]))


def to_bytes(dfs: dict) -> dict:
    return {t: dfs[t].to_csv(index=False).encode() for t in dfs}


def full_pipeline(raw_bytes: dict):
    """What the app does with an upload: validate -> clean -> score -> SQL store -> one query."""
    from core.qa import DataStore
    from core.scoring import apply_weights, extract_signals, suggest_action
    v = validate_tables(raw_bytes)
    assert v.ok, f"upload rejected: {v.errors}"
    r = clean(*[v.tables[t] for t in TABLES])
    s = apply_weights(extract_signals(r.leads, r.deals, r.activity, r.notes, r.ref_date))
    [suggest_action(x) for x in s.head(20).to_dict("records")]
    store = DataStore(r, s)
    store.run_sql("SELECT COUNT(*) AS n FROM lead_scores")
    return v, r, s


# =============================================================================================
print(f"Data-layer tests – {ROOT}\nSample data: {len(RAW['leads']):,} raw leads, cleaned in {CLEAN_SECS:.1f}s\n")

# ---- A. interface contract -------------------------------------------------------------------
@check("A", "clean() returns CleanResult with all agreed fields")
def _():
    for f in ["leads", "deals", "activity", "notes", "merges", "health", "ref_date"]:
        assert hasattr(RES, f), f"missing .{f}"
    assert isinstance(RES.ref_date, pd.Timestamp) and not pd.isna(RES.ref_date), f"ref_date is {RES.ref_date!r}"
    return f"ref_date {RES.ref_date.date()}"


@check("A", "clean leads have every column scoring / Q&A / app use")
def _():
    missing = LEAD_COLS - set(L.columns)
    assert not missing, f"missing columns: {sorted(missing)}"
    assert pd.api.types.is_bool_dtype(L["is_stale"]), f"is_stale dtype {L['is_stale'].dtype}"
    assert pd.api.types.is_numeric_dtype(L["company_size"]), f"company_size dtype {L['company_size'].dtype}"


@check("A", "deals / activity / notes keep CSV columns + original_lead_id")
def _():
    for t, cols in CHILD_COLS.items():
        df = getattr(RES, t)
        missing = set(cols + ["original_lead_id"]) - set(df.columns)
        assert not missing, f"{t} missing {sorted(missing)}"
    for t, c in [("deals", "expected_close_date"), ("activity", "activity_date"), ("notes", "note_date")]:
        assert pd.api.types.is_datetime64_any_dtype(getattr(RES, t)[c]), f"{t}.{c} is not a datetime"


@check("A", "merge log has the agreed columns")
def _():
    need = {"duplicate_id", "kept_id", "rule", "evidence", "match_score"}
    assert need <= set(RES.merges.columns), f"missing {sorted(need - set(RES.merges.columns))}"
    return f"{len(RES.merges)} merges, rules: {dict(RES.merges['rule'].value_counts())}"


@check("A", "health dict has every key the app reads")
def _():
    missing = APP_HEALTH_KEYS - set(RES.health)
    assert not missing, f"missing {sorted(missing)}"


# ---- B. data integrity ------------------------------------------------------------------------
@check("B", "lead_id is unique after cleaning")
def _():
    d = L["lead_id"][L["lead_id"].duplicated()]
    assert d.empty, f"repeated ids: {list(d.head())}"


@check("B", "no deal / activity / note rows lost or added")
def _():
    for t in ["deals", "activity", "notes"]:
        assert len(getattr(RES, t)) == len(RAW[t]), f"{t}: {len(RAW[t])} raw -> {len(getattr(RES, t))} clean"


@check("B", "every child row points to a surviving lead (no orphans)")
def _():
    ids = set(L["lead_id"])
    bad = {t: int((~getattr(RES, t)["lead_id"].isin(ids)).sum()) for t in ["deals", "activity", "notes"]}
    assert not any(bad.values()), f"orphans: {bad}"


@check("B", "child rows re-linked correctly (original id -> its survivor)")
def _():
    owner = {m: k for k, g in groups_of(RES).items() for m in g}
    wrong = 0
    for t in ["deals", "activity", "notes"]:
        df = getattr(RES, t)
        assert (df["original_lead_id"].values == RAW[t]["lead_id"].values).all(), f"{t}: original_lead_id was changed"
        wrong += int((df["original_lead_id"].map(owner) != df["lead_id"]).sum())
    assert wrong == 0, f"{wrong} child rows attached to the wrong lead"


@check("B", "merge log, merged_from and the counts all agree")
def _():
    g = groups_of(RES)
    folded = {m for k, s in g.items() for m in s if m != k}
    log = set(RES.merges["duplicate_id"])
    assert folded == log, f"merged_from has {len(folded - log)} ids not in the log, log has {len(log - folded)} not in merged_from"
    assert not (log & set(L["lead_id"])), "a merged-away id is still a lead"
    assert set(RES.merges["kept_id"]) <= set(L["lead_id"]), "a kept_id is not a surviving lead"
    n = len(RAW["leads"]) - len(L)
    assert n == len(log) == RES.health["duplicates_merged"], f"raw-clean={n}, log={len(log)}, health={RES.health['duplicates_merged']}"
    assert (L["n_sources"] == L["lead_id"].map(lambda x: len(g[x]))).all(), "n_sources disagrees with merged_from"
    return f"{n} duplicates folded"


@check("B", "health numbers match the data")
def _():
    h = RES.health
    assert h["clean_records"] == len(L)
    assert h["stale_records"] == int(L["is_stale"].sum())
    assert h["deals"] == len(RES.deals) and h["activities"] == len(RES.activity) and h["notes"] == len(RES.notes)
    assert h.get("orphan_deals", 0) == 0, f"orphan_deals={h.get('orphan_deals')}"


@check("B", "is_stale  <=>  stale_reason is filled in")
def _():
    bad = L[L["is_stale"] != (L["stale_reason"].fillna("") != "")]
    assert bad.empty, f"{len(bad)} leads disagree, e.g. {list(bad['lead_id'].head(3))}"


@check("B", "days_since_contact is a whole number >= 0 for every lead")
def _():
    d = L["days_since_contact"]
    assert d.notna().all(), f"{int(d.isna().sum())} leads have no days_since_contact"
    assert (d >= 0).all(), f"{int((d < 0).sum())} leads were contacted 'in the future'"


@check("B", "merged lead keeps the LATEST contact date of its records")
def _():
    raw_last = pd.to_datetime(RAW["leads"].set_index("lead_id")["last_contact_date"], errors="coerce")
    bad = [k for k, s in groups_of(RES).items() if len(s) > 1
           and L.loc[L["lead_id"] == k, "last_contact_date"].iloc[0] < raw_last.reindex(list(s)).max()]
    assert not bad, f"{len(bad)} merged leads lost their latest contact date, e.g. {bad[:3]}"


@check("B", "emails normalised (lower-case, trimmed), names trimmed")
def _():
    e = L["email"].fillna("").astype(str)
    assert (e == e.str.strip().str.lower()).all(), f"{int((e != e.str.strip().str.lower()).sum())} emails not normalised"
    n = L["name"].astype(str)
    clean_n = n.str.strip().str.replace(r"\s+", " ", regex=True)
    assert (n == clean_n).all(), f"{int((n != clean_n).sum())} names with stray spaces"


@check("B", "review list: real, unmerged, unique pairs")
def _():
    r = RES.review if RES.review is not None else pd.DataFrame(columns=["lead_a", "lead_b"])
    owner = {m: k for k, g in groups_of(RES).items() for m in g}
    same = r[r["lead_a"].map(owner) == r["lead_b"].map(owner)]
    assert same.empty, f"{len(same)} review pairs were actually merged"
    unknown = set(r["lead_a"]) | set(r["lead_b"]) - set(RAW["leads"]["lead_id"])
    assert not (unknown - set(RAW["leads"]["lead_id"])), "review list mentions unknown ids"
    pairs = r.apply(lambda x: tuple(sorted([x["lead_a"], x["lead_b"]])), axis=1)
    assert not pairs.duplicated().any(), "the same pair is listed twice"
    return f"{len(r)} pairs for review"


# ---- C. dedupe vs ground truth (computed here, independently) ---------------------------------
def truth_clusters():
    parent = {}

    def f(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            x = parent[x]
        return x
    for a, b in zip(TRUTH["duplicate_id"], TRUTH["original_id"]):
        parent[f(a)] = f(b)
    return f


@check("C", "ground truth covers real records (plan task 1)")
def _():
    raw_ids = set(RAW["leads"]["lead_id"])
    missing = (set(TRUTH["duplicate_id"]) | set(TRUTH["original_id"])) - raw_ids
    assert not missing, f"truth mentions ids not in leads.csv: {sorted(missing)[:5]}"
    assert not TRUTH["duplicate_id"].duplicated().any(), "a duplicate_id is listed twice"
    return f"{len(TRUTH)} injected duplicates recorded"


@check("C", "recall >= 98% (plan target)")
def _():
    owner = {m: k for k, g in groups_of(RES).items() for m in g}
    found = sum(owner[a] == owner[b] for a, b in zip(TRUTH["duplicate_id"], TRUTH["original_id"]))
    rec = found / len(TRUTH)
    assert rec >= 0.98, f"recall {rec:.1%} ({found}/{len(TRUTH)})"
    return f"recall {rec:.1%} ({found}/{len(TRUTH)}) – margin: {found - int(0.98 * len(TRUTH) + 0.999)} pair(s) above target"


@check("C", "zero false merges (plan target)")
def _():
    f = truth_clusters()
    bad = [(k, sorted(s)) for k, s in groups_of(RES).items() if len({f(x) for x in s}) > 1]
    assert not bad, f"{len(bad)} groups join different people, e.g. {bad[:2]}"


@check("C", "the OLDEST record survives a merge")
def _():
    created = pd.to_datetime(RAW["leads"].set_index("lead_id")["created_at"], errors="coerce")
    bad = []
    for k, s in groups_of(RES).items():
        if len(s) > 1:
            oldest = min(s, key=lambda x: (created.get(x), x))
            if oldest != k:
                bad.append((k, oldest))
    assert not bad, f"{len(bad)} groups kept a newer record, e.g. {bad[:3]}"


# ---- D. determinism ---------------------------------------------------------------------------
@check("D", "cleaning twice gives identical results")
def _():
    r2 = clean(*[RAW[t] for t in TABLES])
    pd.testing.assert_frame_equal(r2.leads.reset_index(drop=True), L.reset_index(drop=True))
    assert r2.health["duplicates_merged"] == RES.health["duplicates_merged"]


@check("D", "shuffling the input rows gives the same merges")
def _():
    shuffled = [RAW[t].sample(frac=1, random_state=7).reset_index(drop=True) for t in TABLES]
    r2 = clean(*shuffled)
    a = {frozenset(s) for s in groups_of(RES).values() if len(s) > 1}
    b = {frozenset(s) for s in groups_of(r2).values() if len(s) > 1}
    assert a == b, f"{len(a ^ b)} merge groups differ when rows arrive in another order"
    assert set(r2.leads["lead_id"]) == set(L["lead_id"]), "different surviving ids"


# ---- E. entity-resolution edge cases (known right answers) ------------------------------------
def merged(res, *ids):
    owner = {m: k for k, g in groups_of(res).items() for m in g}
    return len({owner[i] for i in ids}) == 1


@check("E", "same email with different case / spaces -> merged")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                       lead("L2", "Priya Sharma", "Acme Inc.", "  PRIYA@ACME.COM ", created="2025-06-01")]))
    assert merged(r, "L1", "L2")


@check("E", "'Acme Inc.' vs 'ACME Corporation', 'P. Sharma' vs 'Priya Sharma' -> merged")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                       lead("L2", "P. Sharma", "ACME Corporation", "", created="2025-06-01")]))
    assert merged(r, "L1", "L2")


@check("E", "chain: A=B by email, B=C by company+name -> one lead")
def _():
    r = clean(*tables([lead("L1", "Rahul Verma", "Zeta Ltd", "rahul@zeta.com"),
                       lead("L2", "Rahul Verma", "Zeta Ltd", "rahul@zeta.com", created="2025-03-01"),
                       lead("L3", "R. Verma", "ZETA LLC", "", created="2025-06-01")]))
    assert merged(r, "L1", "L2", "L3") and len(r.leads) == 1


@check("E", "duplicate's deals / activity / notes move to the survivor")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                       lead("L2", "PRIYA SHARMA", "acme", "priya@acme.com", created="2025-06-01")],
                      deals=[{"deal_id": "D1", "lead_id": "L2", "deal_name": "x", "amount_usd": 5000, "stage": "Qualified",
                              "expected_close_date": "2026-10-20", "last_stage_change": "2026-09-01"}],
                      activity=[{"activity_id": "A1", "lead_id": "L2", "type": "meeting", "activity_date": "2026-09-10"}]))
    assert set(r.deals["lead_id"]) == {"L1"} and set(r.activity["lead_id"]) == {"L1"}
    assert r.leads.iloc[0]["name"] == "Priya Sharma", f"kept name {r.leads.iloc[0]['name']!r} (should prefer non-ALL-CAPS)"


@check("E", "two different 'John Smith's at different companies -> NOT merged")
def _():
    r = clean(*tables([lead("L1", "John Smith", "Acme Inc.", "john@acme.com", industry="FinTech", country="India", size=250),
                       lead("L2", "John Smith", "Globex Ltd", "jsmith@globex.com", industry="Retail", country="UK", size=50,
                            created="2025-06-01")]))
    assert not merged(r, "L1", "L2"), "merged two different people"


@check("E", "different people at the same company -> NOT merged")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                       lead("L2", "Rohan Mehta", "Acme Inc.", "rohan@acme.com", created="2025-06-01"),
                       lead("L3", "Priya Shah", "Acme Inc.", "pshah@acme.com", created="2025-07-01")]))
    assert not merged(r, "L1", "L2") and not merged(r, "L1", "L3"), "merged colleagues"


@check("E", "two people sharing a team inbox (info@) are NOT merged", level_on_fail="WARN")
def _():
    r = clean(*tables([lead("L1", "Alice Wong", "Acme Inc.", "info@acme.com"),
                       lead("L2", "Bob Lee", "Acme Inc.", "info@acme.com", created="2025-06-01")]))
    assert not merged(r, "L1", "L2"), ("'same email' merges Alice Wong and Bob Lee who share info@acme.com – "
                                       "consider also requiring a similar name for generic inboxes")


@check("E", "malformed email is flagged invalid / unreachable")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@@acme"),
                       lead("L2", "Rohan Mehta", "Beta Ltd", "rohan@beta.com")]))
    p = r.leads.set_index("lead_id").loc["L1"]
    assert p["email_status"] == "invalid" and p["is_stale"], f"status {p['email_status']}, stale {p['is_stale']}"


@check("E", "cleaning flags a lead with NO contact date (not 'recently contacted')", level_on_fail="WARN")
def _():
    l, d, a, n = tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                         lead("L2", "Rohan Mehta", "Beta Ltd", "rohan@beta.com", last="")])
    v = validate_tables(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    r = clean(*[v.tables[t] for t in TABLES])
    p = r.leads.set_index("lead_id").loc["L2"]
    assert p["is_stale"] or "contact" in str(p["stale_reason"]), (
        "clean() leaves days_since_contact empty and is_stale=False – suggest stale_reason 'no contact date' "
        "(scoring now guards against it, but the Data health tab and SQL answers still treat it as fresh)")
    warned = any("last_contact_date" in w for w in v.warnings)
    assert warned, "upload validation accepts blank last_contact_date values without a warning"


@check("E", "blank name / company in a lead -> no crash")
def _():
    r = clean(*tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                       lead("L2", None, None, "x@y.com"), lead("L3", "", "", "", created="2025-06-01")]))
    assert len(r.leads) == 3


# ---- F. upload validation ---------------------------------------------------------------------
SAMPLE_BYTES = {t: (DATA / f"{t}.csv").read_bytes() for t in TABLES}


@check("F", "sample files pass with no errors")
def _():
    v = validate_tables(SAMPLE_BYTES)
    assert v.ok, v.errors
    return f"{len(v.warnings)} warning(s)"


@check("F", "header variants (Email, E-mail, Company Name, Lead ID, Deal Value…) accepted")
def _():
    dfs = {t: pd.read_csv(DATA / f"{t}.csv") for t in TABLES}
    dfs["leads"] = dfs["leads"].rename(columns={"email": "E-mail", "company": "Company Name", "lead_id": "Lead ID",
                                                "name": "Full Name", "company_size": "Employees"})
    dfs["deals"] = dfs["deals"].rename(columns={"amount_usd": "Deal Value", "expected_close_date": "Close Date"})
    dfs["activity"] = dfs["activity"].rename(columns={"type": "Activity Type", "activity_date": "Date"})
    v = validate_tables(to_bytes(dfs))
    assert v.ok, v.errors
    assert v.tables["leads"]["email"].equals(pd.read_csv(DATA / "leads.csv", dtype=str, keep_default_na=False)["email"].str.strip()), \
        "email values changed while renaming"
    return f"renamed: {sum(len(m) for m in v.renamed.values())} columns"


@check("F", "missing required column -> clear error naming file + column")
def _():
    dfs = {t: pd.read_csv(DATA / f"{t}.csv") for t in TABLES}
    dfs["leads"] = dfs["leads"].drop(columns=["email"])
    v = validate_tables(to_bytes(dfs))
    assert not v.ok and any("leads.csv" in e and "email" in e for e in v.errors), v.errors


@check("F", "garbage / empty / missing files -> errors, never a crash")
def _():
    for bad in [b"\x89PNG\r\n\x1a\n\x00\x00garbage\xff\xfe", b"", b"lead_id,name\n"]:
        v = validate_tables({**SAMPLE_BYTES, "leads": bad})
        assert not v.ok and any("leads.csv" in e for e in v.errors), f"{bad[:12]!r}: {v.errors}"
    v = validate_tables({t: SAMPLE_BYTES[t] for t in TABLES if t != "notes"})
    assert not v.ok and any("notes.csv" in e for e in v.errors)


@check("F", "messy values: '$12,000', 'negotiation', 'Demo Request', size 'lots'")
def _():
    l, d, a, n = tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com", size="lots")],
                        deals=[{"deal_id": "D1", "lead_id": "L1", "deal_name": "x", "amount_usd": "$12,000", "stage": "negotiation",
                                "expected_close_date": "2026-10-20", "last_stage_change": "2026-09-01"}],
                        activity=[{"activity_id": "A1", "lead_id": "L1", "type": "Demo Request", "activity_date": "2026-09-10"}])
    v = validate_tables(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    assert v.ok, v.errors
    assert int(v.tables["deals"]["amount_usd"].iloc[0]) == 12000, v.tables["deals"]["amount_usd"].iloc[0]
    assert v.tables["deals"]["stage"].iloc[0] == "Negotiation"
    assert v.tables["activity"]["type"].iloc[0] == "demo_request"
    assert int(v.tables["leads"]["company_size"].iloc[0]) == 0 and v.warnings


# ---- G. end to end: upload -> clean -> score -> SQL --------------------------------------------
@check("G", "sample upload with renamed headers runs the full pipeline")
def _():
    dfs = {t: pd.read_csv(DATA / f"{t}.csv") for t in TABLES}
    dfs["leads"] = dfs["leads"].rename(columns={"email": "Email", "company": "Company"})
    _, r, s = full_pipeline(to_bytes(dfs))
    return f"{len(s):,} leads scored"


@check("G", "upload with NO deals at all still works")
def _():
    l, d, a, n = tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                         lead("L2", "Rohan Mehta", "Beta Ltd", "rohan@beta.com")])
    _, r, s = full_pipeline(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    assert len(s) == 2


@check("G", "upload where some leads have a blank last_contact_date still works")
def _():
    l, d, a, n = tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com"),
                         lead("L2", "Rohan Mehta", "Beta Ltd", "rohan@beta.com", last="")])
    v, r, s = full_pipeline(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    row = s.set_index("lead_id").loc["L2"]
    recency = [c for c in row["components"] if c["component"] == "Data quality / recency"]
    assert row["is_stale"] or (recency and recency[0]["points"] < 0), \
        "a lead with NO contact date is scored as if it was contacted recently"
    return f"L2 scored {row['score']} with '{recency[0]['fact'] if recency else 'stale'}'"


@check("G", "upload with a few non-date values in last_contact_date still works")
def _():
    rows = [lead(f"L{i}", f"Person {chr(65 + i)} Kumar{i}", f"Firm{i} Ltd", f"p{i}@firm{i}.com") for i in range(10)]
    rows[3]["last_contact_date"] = "last tuesday"
    l, d, a, n = tables(rows)
    v, r, s = full_pipeline(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    assert any("last_contact_date" in w for w in v.warnings), f"no warning about the bad date: {v.warnings}"


@check("G", "child rows pointing to unknown leads -> warning, pipeline still works")
def _():
    l, d, a, n = tables([lead("L1", "Priya Sharma", "Acme Inc.", "priya@acme.com")],
                        deals=[{"deal_id": "D1", "lead_id": "L999", "deal_name": "x", "amount_usd": 5000, "stage": "Qualified",
                                "expected_close_date": "2026-10-20", "last_stage_change": "2026-09-01"}])
    v, r, s = full_pipeline(to_bytes({"leads": l, "deals": d, "activity": a, "notes": n}))
    assert any("L999" in w or "not in leads.csv" in w for w in v.warnings), v.warnings


# ---- H. scale ---------------------------------------------------------------------------------
@check("H", "5,000-lead generated CRM cleans + scores in < 60 s", level_on_fail="WARN")
def _():
    tmp = Path(tempfile.mkdtemp())
    shutil.copy(DATA / "generate_data.py", tmp / "generate_data.py")
    subprocess.run([sys.executable, str(tmp / "generate_data.py"), "--leads", "5000", "--seed", "3"],
                   check=True, capture_output=True, cwd=tmp)
    from core.scoring import apply_weights, extract_signals
    raw = [pd.read_csv(tmp / f"{t}.csv") for t in TABLES]
    t = time.time()
    r = clean(*raw)
    c = time.time() - t
    apply_weights(extract_signals(r.leads, r.deals, r.activity, r.notes, r.ref_date))
    total = time.time() - t
    shutil.rmtree(tmp, ignore_errors=True)
    assert total < 60, f"took {total:.0f}s (clean {c:.0f}s) – the hosted app would feel stuck on first load"
    return f"{len(raw[0]):,} raw leads: clean {c:.1f}s, clean+score {total:.1f}s"


# =============================================================================================
n = {s: sum(r[2] == s for r in results) for s in ["PASS", "FAIL", "WARN"]}
print(f"\n{n['PASS']} passed · {n['FAIL']} failed · {n['WARN']} warnings  (of {len(results)} tests)")
for g, name, s, d in results:
    if s != "PASS":
        print(f"  {s}  {g}  {name}\n        {d}")
sys.exit(1 if n["FAIL"] else 0)
