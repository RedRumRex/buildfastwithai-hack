"""
Data ingestion + cleaning layer.

- Normalises names, companies, emails and phone numbers
- Finds duplicate leads (entity resolution) and merges them, re-pointing their
  deals / activity / notes to the surviving record
- Puts borderline pairs in a "possible duplicate: needs review" list instead of merging them
- Flags stale / unreachable records
- Produces a data-health report (before/after + field-level issues) and a merge log so every
  change is traceable
"""
import re
from dataclasses import dataclass, field

import pandas as pd
from rapidfuzz import fuzz

STALE_DAYS = 180
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$")
COMPANY_SUFFIXES = r"\b(inc|corp|corporation|ltd|llc|pvt|private|limited|technologies|solutions|group|co)\b\.?"


# suffixes that can also appear glued onto the name ("WalshPvt Inc."); short ones like "co" are left out
# on purpose, since stripping them from the end of a word would break real names ("Franco" -> "fran")
GLUED_SUFFIXES = ("corporation", "technologies", "solutions", "limited", "private", "group", "corp", "pvt", "ltd", "llc", "inc")


def norm_company(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(COMPANY_SUFFIXES, " ", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s


def company_key(s: str) -> str:
    """Blocking key for dedupe: norm_company, plus suffixes glued to the end of the name are stripped."""
    k = norm_company(s)
    stripped = True
    while stripped:
        stripped = False
        for suf in GLUED_SUFFIXES:
            if k.endswith(suf) and len(k) - len(suf) >= 3:
                k, stripped = k[: -len(suf)], True
                break
    return k


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower().replace(".", ""))


def norm_email(s) -> str:
    s = "" if pd.isna(s) else str(s)
    return s.strip().lower()


def valid_email(s: str) -> bool:
    """Format check on a normalised email ('' counts as missing, not invalid)."""
    return bool(EMAIL_RE.match(s))


def norm_phone(s) -> str:
    """Digits only, extension dropped, last 10 digits (so '+91 94529 18147' == '9452918147')."""
    s = "" if pd.isna(s) else str(s)
    digits = re.sub(r"\D", "", re.split(r"x|ext", s.lower())[0])
    return digits[-10:] if len(digits) >= 7 else ""


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
    review: pd.DataFrame = None     # possible duplicates NOT merged automatically: a person decides


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

    leads["_n_company"] = leads["company"].map(company_key)
    leads["_n_name"] = leads["name"].map(norm_name)
    leads["_n_email"] = leads["email"].map(norm_email)
    leads["_n_phone"] = leads["phone"].map(norm_phone)
    leads["_last"] = leads["_n_name"].str.split().str[-1].fillna("")
    raw_leads = leads.copy()
    missing_email = int((leads["_n_email"] == "").sum())

    # ---- entity resolution ---------------------------------------------------
    # keep the oldest record as the "master"; duplicates are folded into it
    leads = leads.sort_values(["created_at", "lead_id"]).reset_index(drop=True)
    parent = {lid: lid for lid in leads["lead_id"]}
    merges, review = [], []

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def link(keep, dup, rule, evidence, score):
        if find(keep) != find(dup):
            parent[find(dup)] = find(keep)
            merges.append({"duplicate_id": dup, "kept_id": keep, "rule": rule, "evidence": evidence, "match_score": score})

    cols = ["lead_id", "_n_name", "name", "company", "_n_email", "_n_phone", "_n_company", "industry", "country", "company_size"]

    def pairs_within(key):
        """Candidate pairs that share a blocking key (older record first)."""
        for k, grp in leads[leads[key] != ""].groupby(key, sort=False):
            rows = grp[cols].to_dict("records")
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    yield rows[i], rows[j]

    def who(r):
        return f"'{str(r['name']).strip()}' @ '{r['company']}'"

    # rule 1: identical (normalised) email
    by_email = leads[leads["_n_email"] != ""].groupby("_n_email")["lead_id"].apply(list)
    for email, ids in by_email.items():
        for dup in ids[1:]:
            link(ids[0], dup, "same email", email, 100)

    # rule 2: same normalised company + fuzzy / initial name match (blocking by company)
    by_initial = {}   # 'M. Newton' matched only through an initial: merge later, if it is unambiguous
    for a, b in pairs_within("_n_company"):
        ok, score = same_person(a["_n_name"], b["_n_name"])
        if ok and score < 90:
            short, full = (a, b) if len(a["_n_name"].split()[0]) == 1 else (b, a)
            by_initial.setdefault(short["lead_id"], (short, []))[1].append((full, a is short, score))
        elif ok:
            link(a["lead_id"], b["lead_id"], "same company + similar name", f"{who(b)}  ~  {who(a)}", score)
        elif score >= 75 and find(a["lead_id"]) != find(b["lead_id"]):
            review.append({"lead_a": a["lead_id"], "lead_b": b["lead_id"], "reason": "same company, name only partly similar",
                           "evidence": f"{who(b)}  ?  {who(a)}", "match_score": score})
    for short, cands in by_initial.values():
        people = {find(full["lead_id"]) for full, _, _ in cands}
        for full, short_is_older, score in cands:
            older, newer = (short, full) if short_is_older else (full, short)
            if len(people) == 1:
                link(older["lead_id"], newer["lead_id"], "same company + similar name", f"{who(newer)}  ~  {who(older)}", score)
            else:   # 'M. Newton' could be Michael or (Mrs) Amanda Newton -> a person decides
                review.append({"lead_a": older["lead_id"], "lead_b": newer["lead_id"],
                               "reason": f"initial matches {len(people)} different people at this company",
                               "evidence": f"{who(newer)}  ?  {who(older)}", "match_score": score})

    # rule 3: same phone number + similar name (catches renamed companies)
    for a, b in pairs_within("_n_phone"):
        ok, score = same_person(a["_n_name"], b["_n_name"])
        if ok:
            link(a["lead_id"], b["lead_id"], "same phone + similar name", f"{who(b)}  ~  {who(a)}  ☎ {a['_n_phone']}", score)

    # rule 4: near-identical email (a typo) + similar name, compared within the same surname
    for a, b in pairs_within("_last"):
        if not (a["_n_email"] and b["_n_email"]) or find(a["lead_id"]) == find(b["lead_id"]):
            continue
        # a typo keeps the domain (or breaks the format); a different domain is a different mailbox
        same_domain = a["_n_email"].split("@")[-1] == b["_n_email"].split("@")[-1]
        if not (same_domain or not valid_email(a["_n_email"]) or not valid_email(b["_n_email"])):
            continue
        ok, score = same_person(a["_n_name"], b["_n_name"])
        email_score = fuzz.ratio(a["_n_email"], b["_n_email"])
        if ok and email_score >= 90:
            link(a["lead_id"], b["lead_id"], "near-identical email + similar name",
                 f"{b['_n_email']}  ~  {a['_n_email']}", int(email_score))

    # rule 5 + review band: the same person by name at a different company. Two independent hints
    # (same email name AND same industry / country / size) -> merge; only one hint -> a person decides
    for a, b in pairs_within("_last"):
        if find(a["lead_id"]) == find(b["lead_id"]):
            continue
        ok, score = same_person(a["_n_name"], b["_n_name"])
        if not ok:
            continue
        hints = []
        if a["_n_email"] and b["_n_email"] and a["_n_email"].split("@")[0] == b["_n_email"].split("@")[0]:
            hints.append("same email name, different domain")
        if (a["industry"], a["country"], a["company_size"]) == (b["industry"], b["country"], b["company_size"]):
            hints.append("same industry, country and company size")
        if len(hints) == 2:
            link(a["lead_id"], b["lead_id"], "similar name + same email name + same firmographics",
                 f"{who(b)}  ~  {who(a)}", score)
        elif hints:
            review.append({"lead_a": a["lead_id"], "lead_b": b["lead_id"],
                           "reason": "similar name at a different company: " + "; ".join(hints),
                           "evidence": f"{who(b)}  ?  {who(a)}", "match_score": score})

    leads["master_id"] = leads["lead_id"].map(find)
    merges_df = pd.DataFrame(merges, columns=["duplicate_id", "kept_id", "rule", "evidence", "match_score"])
    if len(merges_df):
        merges_df["kept_id"] = merges_df["kept_id"].map(find)
    review_df = pd.DataFrame(review, columns=["lead_a", "lead_b", "reason", "evidence", "match_score"])
    review_df = review_df[review_df["lead_a"].map(find) != review_df["lead_b"].map(find)]
    review_df = review_df.drop_duplicates(["lead_a", "lead_b"]).sort_values("match_score", ascending=False).reset_index(drop=True)

    # ---- merge fields: fill gaps from duplicates, keep latest contact -------------
    conflicts = {"title": 0, "phone": 0}

    def combine(mid: str, g: pd.DataFrame) -> dict:
        master = g[g["lead_id"] == mid].iloc[0].to_dict()
        emails = [e for e in g["_n_email"] if e]
        good = [e for e in emails if valid_email(e)]
        # a well-formed email beats a mistyped one; otherwise keep the master's own
        master["email"] = (master["_n_email"] if valid_email(master["_n_email"]) else None) or \
            (good[0] if good else master["_n_email"] or (emails[0] if emails else ""))
        name = re.sub(r"\s+", " ", str(master["name"]).strip())
        if name.isupper():   # prefer a variant that isn't SHOUTED; fall back to Title Case
            mixed = [re.sub(r"\s+", " ", str(n).strip()) for n in g["name"] if not str(n).strip().isupper()]
            name = next((n for n in mixed if norm_name(n) == norm_name(name)), name.title())
        master["name"] = name
        phones = [p for p in g["phone"] if norm_phone(p)]
        if not norm_phone(master["phone"]) and phones:
            master["phone"] = phones[0]
        if len(g) > 1:
            conflicts["title"] += g["title"].dropna().nunique() > 1
            conflicts["phone"] += g["_n_phone"][g["_n_phone"] != ""].nunique() > 1
        master["last_contact_date"] = g["last_contact_date"].max()
        same_email = g[g["_n_email"] == master["email"]]
        if master["email"] and not valid_email(master["email"]):
            master["email_status"] = "invalid"
        else:
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
        elif r["email_status"] == "invalid":
            reasons.append("invalid email format")
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
        "possible_duplicates": len(review_df),
        "missing_email": missing_email,
        "bounced_email": int((clean_leads["email_status"] == "bounced").sum()),
        "invalid_email": int((clean_leads["email_status"] == "invalid").sum()),
        "stale_records": int(clean_leads["is_stale"].sum()),
        "stale_pct": round(100 * clean_leads["is_stale"].mean(), 1),
        "deals": len(deals),
        "activities": len(activity),
        "notes": len(notes),
        "orphan_deals": int((~deals["lead_id"].isin(clean_leads["lead_id"])).sum()),
        "conflicts_resolved": {k: int(v) for k, v in conflicts.items()},
        "field_issues": field_issues(raw_leads, clean_leads, deals, activity, ref_date),
    }
    health["before_after"] = before_after(raw_leads, clean_leads, health, ref_date)
    return CleanResult(clean_leads, deals, activity, notes, merges_df, health, ref_date, review_df)


# ---- data-health report ------------------------------------------------------------
ACTIVITY_TYPES = {"email_open", "email_click", "pricing_page_visit", "call", "meeting", "demo_request"}


def _ids(df: pd.DataFrame, mask, id_col: str = "lead_id", n: int = 5) -> str:
    return ", ".join(df.loc[mask, id_col].astype(str).head(n))


def field_issues(raw: pd.DataFrame, clean_leads: pd.DataFrame, deals: pd.DataFrame, activity: pd.DataFrame,
                 ref_date) -> list[dict]:
    """One row per field-level problem: how many records had it before cleaning and after, with example
    IDs (from the clean data when the problem remains, else from the raw data) so it can be traced."""
    c_email = clean_leads["email"].map(norm_email)
    c_name = clean_leads["name"].astype(str)
    r_name = raw["name"].astype(str)
    checks = [
        ("email", "missing", raw["_n_email"] == "", c_email == ""),
        ("email", "invalid format", (raw["_n_email"] != "") & ~raw["_n_email"].map(valid_email),
         (c_email != "") & ~c_email.map(valid_email)),
        ("email", "bounced", raw["email_status"] == "bounced", clean_leads["email_status"] == "bounced"),
        ("phone", "missing or unreadable", raw["_n_phone"] == "", clean_leads["phone"].map(norm_phone) == ""),
        ("name", "ALL CAPS or stray spaces",
         r_name.str.isupper() | (r_name != r_name.str.strip().str.replace(r"\s+", " ", regex=True)),
         c_name.str.isupper() | (c_name != c_name.str.strip().str.replace(r"\s+", " ", regex=True))),
        ("last_contact_date", "missing or not a date", raw["last_contact_date"].isna(), clean_leads["last_contact_date"].isna()),
        ("last_contact_date", f"older than {STALE_DAYS} days",
         (ref_date - raw["last_contact_date"]).dt.days > STALE_DAYS, clean_leads["days_since_contact"] > STALE_DAYS),
        ("created_at", "missing or not a date", raw["created_at"].isna(), clean_leads["created_at"].isna()),
    ]
    out = []
    for fld, issue, before, after in checks:
        out.append({"table": "leads", "field": fld, "issue": issue, "before": int(before.sum()), "after": int(after.sum()),
                    "example_ids": _ids(clean_leads, after) if after.any() else _ids(raw, before)})
    raw_ids = set(raw["lead_id"])
    orphan_before = ~deals["original_lead_id"].isin(raw_ids)
    orphan_after = ~deals["lead_id"].isin(clean_leads["lead_id"])
    amount = pd.to_numeric(deals["amount_usd"], errors="coerce")
    bad_type = ~activity["type"].isin(ACTIVITY_TYPES)
    out += [
        {"table": "deals", "field": "lead_id", "issue": "points to no lead", "before": int(orphan_before.sum()),
         "after": int(orphan_after.sum()), "example_ids": _ids(deals, orphan_after | orphan_before, "deal_id")},
        {"table": "deals", "field": "amount_usd", "issue": "missing or ≤ 0", "before": int((amount.isna() | (amount <= 0)).sum()),
         "after": int((amount.isna() | (amount <= 0)).sum()), "example_ids": _ids(deals, amount.isna() | (amount <= 0), "deal_id")},
        {"table": "activity", "field": "type", "issue": "unknown activity type", "before": int(bad_type.sum()),
         "after": int(bad_type.sum()), "example_ids": _ids(activity, bad_type, "activity_id")},
    ]
    return out


def before_after(raw: pd.DataFrame, clean_leads: pd.DataFrame, health: dict, ref_date) -> list[dict]:
    """Headline numbers for the raw upload vs the cleaned data."""
    c_email = clean_leads["email"].map(norm_email)
    reachable_raw = (raw["_n_email"] != "") & raw["_n_email"].map(valid_email) & (raw["email_status"] != "bounced")
    reachable = (c_email != "") & c_email.map(valid_email) & (clean_leads["email_status"] == "valid")
    return [
        {"metric": "Lead records", "before": len(raw), "after": len(clean_leads)},
        {"metric": "Duplicate records", "before": health["duplicates_merged"], "after": 0},
        {"metric": "Possible duplicates (needs review)", "before": health["possible_duplicates"], "after": health["possible_duplicates"]},
        {"metric": "Leads with a usable email", "before": int(reachable_raw.sum()), "after": int(reachable.sum())},
        {"metric": "Leads with a phone number", "before": int((raw["_n_phone"] != "").sum()),
         "after": int((clean_leads["phone"].map(norm_phone) != "").sum())},
        {"metric": "Names in ALL CAPS", "before": int(raw["name"].astype(str).str.strip().str.isupper().sum()),
         "after": int(clean_leads["name"].astype(str).str.isupper().sum())},
        {"metric": "Stale / unreachable leads", "before": int((~reachable_raw | ((ref_date - raw["last_contact_date"]).dt.days > STALE_DAYS)).sum()),
         "after": health["stale_records"]},
    ]
