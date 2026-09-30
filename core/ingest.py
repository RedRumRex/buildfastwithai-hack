"""
Upload validation: turn four user-supplied CSVs into tables clean() can rely on, or explain clearly why not.

- Maps header variants to our column names ("Email" / "E-mail" / "email_address" -> email)
- Errors (the upload is rejected, with a message per file) for: unreadable CSV, no rows,
  a required column that is missing, a required date column that is mostly not dates
- Warnings (the upload is used, the user is told) for: optional columns filled with defaults,
  rows without an id, duplicate ids, unparseable dates / amounts, unknown stages or activity types,
  deals / activity / notes that point to no lead
- Normalises stage and activity-type spellings ("demo request" -> demo_request, "negotiation" -> Negotiation)

    result = validate_tables({"leads": b"...", "deals": b"...", "activity": b"...", "notes": b"..."})
    if result.errors: show them
    else: clean(*[result.tables[t] for t in TABLES])
"""
import io
import re
from dataclasses import dataclass, field

import pandas as pd

TABLES = ["leads", "deals", "activity", "notes"]
ID_COL = {"leads": "lead_id", "deals": "deal_id", "activity": "activity_id", "notes": "note_id"}

# column -> default used when an upload doesn't have it (None = required)
SCHEMA = {
    "leads": {"lead_id": None, "name": None, "company": None, "email": None, "phone": "", "title": "",
              "industry": "", "company_size": 0, "country": "", "source": "", "created_at": None,
              "last_contact_date": None, "email_status": "valid"},
    "deals": {"deal_id": None, "lead_id": None, "deal_name": "", "amount_usd": None, "stage": None,
              "expected_close_date": None, "last_stage_change": None},
    "activity": {"activity_id": None, "lead_id": None, "type": None, "activity_date": None},
    "notes": {"note_id": None, "lead_id": None, "note_date": None, "author": "", "text": None},
}
DATE_COLS = {"leads": ["created_at", "last_contact_date"], "deals": ["expected_close_date", "last_stage_change"],
             "activity": ["activity_date"], "notes": ["note_date"]}

# header variants, compared after lower-casing and turning spaces / punctuation into "_"
ALIASES = {
    "leads": {"lead_id": ["id", "leadid", "lead", "lead_no", "lead_number", "contact_id"],
              "name": ["full_name", "contact_name", "lead_name", "contact", "person"],
              "company": ["company_name", "account", "account_name", "organisation", "organization", "org"],
              "email": ["email_address", "e_mail", "mail", "work_email"],
              "phone": ["phone_number", "mobile", "telephone", "tel", "contact_number"],
              "title": ["job_title", "designation", "position", "role"],
              "company_size": ["employees", "employee_count", "size", "headcount", "no_of_employees"],
              "created_at": ["created", "created_date", "date_created", "created_on"],
              "last_contact_date": ["last_contacted", "last_contact", "last_contacted_on", "last_touch"],
              "email_status": ["email_state"]},
    "deals": {"deal_id": ["id", "dealid", "opportunity_id", "opp_id"],
              "lead_id": ["lead", "leadid", "contact_id"],
              "deal_name": ["name", "opportunity", "opportunity_name", "title"],
              "amount_usd": ["amount", "deal_amount", "value", "deal_value", "amount_usd_", "amount_in_usd"],
              "stage": ["deal_stage", "pipeline_stage", "status"],
              "expected_close_date": ["close_date", "expected_close", "closing_date"],
              "last_stage_change": ["stage_changed", "stage_change_date", "last_stage_update", "stage_updated"]},
    "activity": {"activity_id": ["id", "event_id"], "lead_id": ["lead", "leadid", "contact_id"],
                 "type": ["activity_type", "event_type", "event", "kind"],
                 "activity_date": ["date", "event_date", "timestamp", "occurred_at"]},
    "notes": {"note_id": ["id", "noteid"], "lead_id": ["lead", "leadid", "contact_id"],
              "note_date": ["date", "created_at", "created", "note_created"],
              "author": ["owner", "rep", "written_by", "created_by"],
              "text": ["note", "notes", "body", "content", "note_text", "comment"]},
}
STAGES = ["New", "Qualified", "Demo Scheduled", "Proposal Sent", "Negotiation", "Closed Won", "Closed Lost"]
ACTIVITY_TYPES = ["email_open", "email_click", "pricing_page_visit", "call", "meeting", "demo_request"]


@dataclass
class ValidationResult:
    tables: dict = field(default_factory=dict)     # table -> cleaned-up DataFrame (only when there are no errors)
    errors: list = field(default_factory=list)     # "leads.csv: missing required column(s): email"
    warnings: list = field(default_factory=list)
    renamed: dict = field(default_factory=dict)    # table -> {original header: our column}

    @property
    def ok(self) -> bool:
        return not self.errors


def _key(s) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def _canon(value: str, options: list[str]) -> str | None:
    k = _key(value).replace("_", "")
    return next((o for o in options if _key(o).replace("_", "") == k), None)


def map_columns(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, dict, list]:
    """Rename header variants to our column names. Returns (df, {original: new}, warnings)."""
    lookup = {}
    for col in SCHEMA[table]:
        lookup.setdefault(_key(col), col)
        for alias in ALIASES[table].get(col, []):
            lookup.setdefault(_key(alias), col)
    renamed, warnings, seen, keep = {}, [], set(), []
    for orig in df.columns:
        target = lookup.get(_key(orig))
        if target is None:
            keep.append(orig)                 # unknown extra column: kept as is
            continue
        if target in seen:
            warnings.append(f"{table}.csv: '{orig}' also looks like '{target}'; kept the first one and ignored this one")
            continue
        seen.add(target)
        keep.append(orig)
        if orig != target:
            renamed[orig] = target
    return df[keep].rename(columns=renamed), renamed, warnings


def validate_tables(raw: dict) -> ValidationResult:
    """raw: table -> CSV bytes (or a DataFrame). Returns tables with our column names, plus errors / warnings."""
    res = ValidationResult()
    frames = {}
    for t in TABLES:
        src = raw.get(t)
        if src is None:
            res.errors.append(f"{t}.csv: file is missing")
            continue
        try:
            df = src.copy() if isinstance(src, pd.DataFrame) else pd.read_csv(io.BytesIO(src), dtype=str, keep_default_na=False)
        except Exception as e:   # not a CSV, wrong encoding, ...
            res.errors.append(f"{t}.csv: could not be read as a CSV file ({type(e).__name__}: {str(e)[:120]})")
            continue
        if df.empty and t != "deals":   # no deals yet is fine; the rest of the app needs leads, activity, notes
            res.errors.append(f"{t}.csv: has no data rows")
            continue
        df, renamed, warns = map_columns(df, t)
        res.warnings += warns
        if renamed:
            res.renamed[t] = renamed
        missing = [c for c, default in SCHEMA[t].items() if default is None and c not in df.columns]
        if missing:
            res.errors.append(f"{t}.csv: missing required column(s): {', '.join(missing)}. "
                              f"Found: {', '.join(map(str, df.columns))}")
            continue
        for c, default in SCHEMA[t].items():
            if c not in df.columns:
                df[c] = default
                res.warnings.append(f"{t}.csv: no '{c}' column, filled with {default!r}")
        frames[t] = df
    if res.errors:
        return res

    for t, df in frames.items():
        df = df.apply(lambda s: s.str.strip() if s.dtype == object else s)
        idc = ID_COL[t]
        blank = df[idc].astype(str).str.strip() == ""
        if blank.any():
            res.warnings.append(f"{t}.csv: {int(blank.sum())} row(s) without a {idc} were skipped")
            df = df[~blank]
        dup = df[idc].duplicated()
        if dup.any():
            res.warnings.append(f"{t}.csv: {int(dup.sum())} row(s) repeat an existing {idc} "
                                f"(e.g. {', '.join(df.loc[dup, idc].head(3))}); kept the first of each")
            df = df[~dup]
        if df.empty and t != "deals":
            res.errors.append(f"{t}.csv: no rows left after skipping rows without a {idc}")
            continue
        if df.empty:
            res.warnings.append("deals.csv: has no data rows, so no lead gets points for a deal")
        for c in DATE_COLS[t]:
            given = df[c].astype(str).str.strip() != ""
            parsed = pd.to_datetime(df[c], errors="coerce", format="mixed")
            bad = given & parsed.isna()
            if given.sum() and bad.sum() / given.sum() > 0.5:
                res.errors.append(f"{t}.csv: column '{c}' is mostly not dates "
                                  f"(e.g. {', '.join(repr(x) for x in df.loc[bad, c].head(3))})")
            elif bad.any():
                res.warnings.append(f"{t}.csv: {int(bad.sum())} value(s) in '{c}' are not dates and were left empty")
            df[c] = parsed.dt.strftime("%Y-%m-%d").fillna("")
        if t == "leads":
            size = pd.to_numeric(df["company_size"].replace("", 0), errors="coerce")
            if size.isna().any():
                res.warnings.append(f"leads.csv: {int(size.isna().sum())} company_size value(s) are not numbers, set to 0")
            df["company_size"] = size.fillna(0).astype(int)
            status = df["email_status"].str.lower().replace("", "valid")
            df["email_status"] = status.where(status.isin(["valid", "bounced", "invalid"]), "valid")
        if t == "deals":
            amount = pd.to_numeric(df["amount_usd"].astype(str).str.replace(r"[$,\s]", "", regex=True), errors="coerce")
            if amount.isna().any():
                res.warnings.append(f"deals.csv: {int(amount.isna().sum())} amount_usd value(s) are not numbers, set to 0")
            df["amount_usd"] = amount.fillna(0).astype(int)
            canon = df["stage"].map(lambda s: _canon(s, STAGES))
            if canon.isna().any():
                res.warnings.append(f"deals.csv: unknown stage value(s) {sorted(df.loc[canon.isna(), 'stage'].unique())[:5]} "
                                    f"(expected one of {', '.join(STAGES)}); kept as is")
            df["stage"] = canon.fillna(df["stage"])
            df["deal_name"] = df["deal_name"].where(df["deal_name"] != "", df["deal_id"])
        if t == "activity":
            canon = df["type"].map(lambda s: _canon(s, ACTIVITY_TYPES))
            if canon.isna().any():
                res.warnings.append(f"activity.csv: unknown type value(s) {sorted(df.loc[canon.isna(), 'type'].unique())[:5]} "
                                    f"(expected one of {', '.join(ACTIVITY_TYPES)}); they won't count as engagement")
            df["type"] = canon.fillna(df["type"])
        frames[t] = df
    if res.errors:
        return res

    lead_ids = set(frames["leads"]["lead_id"])
    for t in ["deals", "activity", "notes"]:
        orphan = ~frames[t]["lead_id"].isin(lead_ids)
        if orphan.any():
            res.warnings.append(f"{t}.csv: {int(orphan.sum())} row(s) point to a lead_id that is not in leads.csv "
                                f"(e.g. {', '.join(frames[t].loc[orphan, 'lead_id'].head(3))})")
    if (frames["activity"]["activity_date"] == "").all() and (frames["leads"]["last_contact_date"] == "").all():
        res.errors.append("activity.csv / leads.csv: no usable activity_date or last_contact_date, "
                          "so there is no reference date to measure recency from")
        return res
    res.tables = {t: frames[t][list(SCHEMA[t]) + [c for c in frames[t].columns if c not in SCHEMA[t]]] for t in TABLES}
    return res
