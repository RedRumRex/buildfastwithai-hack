"""
Data ingestion + cleaning layer.

- Normalises names, companies and emails
- Finds duplicate leads (entity resolution) and merges them, re-pointing their
  deals / activity / notes to the surviving record
- Flags stale / unreachable records
- Produces a data-health report and a merge log so every change is traceable
"""
import re
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz

STALE_DAYS = 180
COMPANY_SUFFIXES = r"\b(inc|corp|corporation|ltd|llc|pvt|private|limited|technologies|solutions|group|co)\b\.?"


def norm_company(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(COMPANY_SUFFIXES, " ", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower().replace(".", ""))


def norm_email(s) -> str:
    s = "" if pd.isna(s) else str(s)
    return s.strip().lower()


def same_person(a: str, b: str) -> tuple[bool, int]:
    """Fuzzy person match that also understands initials ('P Sharma' vs 'Priya Sharma')."""
    score = fuzz.token_sort_ratio(a, b)
    if score >= 90:
        return True, int(score)
    pa, pb = a.split(), b.split()
    if len(pa) >= 2 and len(pb) >= 2 and pa[-1] == pb[-1]:
        if (len(pa[0]) == 1 and pb[0].startswith(pa[0])) or (len(pb[0]) == 1 and pa[0].startswith(pb[0])):
            return True, 85
    return False, int(score)


@dataclass
class CleanResult:
    leads: pd.DataFrame
    deals: pd.DataFrame
    activity: pd.DataFrame
    notes: pd.DataFrame
    merges: pd.DataFrame            # merge log: which record was folded into which, and why
    health: dict = field(default_factory=dict)
    ref_date: pd.Timestamp = None   # "today" for this dataset


def clean(leads: pd.DataFrame, deals: pd.DataFrame, activity: pd.DataFrame, notes: pd.DataFrame) -> CleanResult:
    leads = leads.copy()
    deals = deals.copy()
    activity = activity.copy()
    notes = notes.copy()

    raw_count = len(leads)
    for c in ["created_at", "last_contact_date"]:
        leads[c] = pd.to_datetime(leads[c], errors="coerce")
    deals["expected_close_date"] = pd.to_datetime(deals["expected_close_date"], errors="coerce")
    deals["last_stage_change"] = pd.to_datetime(deals["last_stage_change"], errors="coerce")
    activity["activity_date"] = pd.to_datetime(activity["activity_date"], errors="coerce")
    notes["note_date"] = pd.to_datetime(notes["note_date"], errors="coerce")

    # reference "today" = day after the latest recorded activity (keeps the demo stable)
    ref_date = max(activity["activity_date"].max(), leads["last_contact_date"].max()) + pd.Timedelta(days=1)

    leads["_n_company"] = leads["company"].map(norm_company)
    leads["_n_name"] = leads["name"].map(norm_name)
    leads["_n_email"] = leads["email"].map(norm_email)
    missing_email = int((leads["_n_email"] == "").sum())

    # ---- entity resolution ---------------------------------------------------
    # keep the oldest record as the "master"; duplicates are folded into it
    leads = leads.sort_values(["created_at", "lead_id"]).reset_index(drop=True)
    parent = {lid: lid for lid in leads["lead_id"]}
    merges = []

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    # rule 1: identical (normalised) email
    by_email = leads[leads["_n_email"] != ""].groupby("_n_email")["lead_id"].apply(list)
    for email, ids in by_email.items():
        for dup in ids[1:]:
            if find(dup) != find(ids[0]):
                parent[find(dup)] = find(ids[0])
                merges.append({"duplicate_id": dup, "kept_id": ids[0], "rule": "same email", "evidence": email, "match_score": 100})

    # rule 2: same normalised company + fuzzy / initial name match (blocking by company)
    for comp, grp in leads.groupby("_n_company"):
        rows = grp[["lead_id", "_n_name", "name", "company"]].values.tolist()
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                ok, score = same_person(rows[i][1], rows[j][1])
                if ok and find(rows[i][0]) != find(rows[j][0]):
                    parent[find(rows[j][0])] = find(rows[i][0])
                    merges.append({
                        "duplicate_id": rows[j][0], "kept_id": rows[i][0],
                        "rule": "same company + similar name",
                        "evidence": f"'{rows[j][2].strip()}' @ '{rows[j][3]}'  ~  '{rows[i][2].strip()}' @ '{rows[i][3]}'",
                        "match_score": score,
                    })

    leads["master_id"] = leads["lead_id"].map(find)
    merges_df = pd.DataFrame(merges, columns=["duplicate_id", "kept_id", "rule", "evidence", "match_score"])
    if len(merges_df):
        merges_df["kept_id"] = merges_df["kept_id"].map(find)

    # ---- merge fields: fill gaps from duplicates, keep latest contact -------------
    def combine(mid: str, g: pd.DataFrame) -> dict:
        master = g[g["lead_id"] == mid].iloc[0].to_dict()
        emails = [e for e in g["_n_email"] if e]
        master["email"] = master["_n_email"] or (emails[0] if emails else "")
        master["name"] = str(master["name"]).strip()
        master["last_contact_date"] = g["last_contact_date"].max()
        same_email = g[g["_n_email"] == master["email"]]
        master["email_status"] = "bounced" if (master["email"] and (same_email["email_status"] == "bounced").any()) else "valid"
        master["merged_from"] = ", ".join(sorted(x for x in g["lead_id"] if x != master["lead_id"]))
        master["n_sources"] = len(g)
        return master

    clean_leads = pd.DataFrame([combine(mid, g) for mid, g in leads.groupby("master_id", sort=False)])
    clean_leads = clean_leads.drop(columns=[c for c in clean_leads.columns if c.startswith("_")])

    # re-point child tables to the surviving lead id
    id_map = dict(zip(leads["lead_id"], leads["master_id"]))
    for df in (deals, activity, notes):
        df["original_lead_id"] = df["lead_id"]
        df["lead_id"] = df["lead_id"].map(id_map).fillna(df["lead_id"])

    # ---- staleness flags ---------------------------------------------------------
    last_act = activity.groupby("lead_id")["activity_date"].max()
    clean_leads["last_activity_date"] = clean_leads["lead_id"].map(last_act)
    # a logged call or meeting is also a contact -> reconcile CRM field with activity log
    touch = activity[activity["type"].isin(["call", "meeting"])].groupby("lead_id")["activity_date"].max()
    clean_leads["last_contact_date"] = pd.concat(
        [clean_leads["last_contact_date"], clean_leads["lead_id"].map(touch)], axis=1).max(axis=1)
    clean_leads["days_since_contact"] = (ref_date - clean_leads["last_contact_date"]).dt.days

    def stale_reason(r):
        reasons = []
        if r["days_since_contact"] > STALE_DAYS:
            reasons.append(f"no contact in {int(r['days_since_contact'])} days")
        if not r["email"]:
            reasons.append("missing email")
        elif r["email_status"] == "bounced":
            reasons.append("email bounced")
        return "; ".join(reasons)

    clean_leads["stale_reason"] = clean_leads.apply(stale_reason, axis=1)
    clean_leads["is_stale"] = clean_leads["stale_reason"] != ""
    clean_leads["merged_from"] = clean_leads["merged_from"].fillna("")

    health = {
        "raw_records": raw_count,
        "clean_records": len(clean_leads),
        "duplicates_merged": raw_count - len(clean_leads),
        "missing_email": missing_email,
        "bounced_email": int((clean_leads["email_status"] == "bounced").sum()),
        "stale_records": int(clean_leads["is_stale"].sum()),
        "stale_pct": round(100 * clean_leads["is_stale"].mean(), 1),
        "deals": len(deals),
        "activities": len(activity),
        "notes": len(notes),
        "orphan_deals": int((~deals["lead_id"].isin(clean_leads["lead_id"])).sum()),
    }
    return CleanResult(clean_leads, deals, activity, notes, merges_df, health, ref_date)
