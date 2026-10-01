"""
Decision agent – transparent lead scoring.

The SCORE is computed by explicit, auditable rules (never by the LLM).
Every component carries the evidence (row ids) that produced it, so the UI can
show a "Why?" panel that traces back to the data. The LLM is only used later to
turn these facts into a readable sentence.

Scoring runs in two steps so a manager can re-weight the rubric live:
  1. extract_signals(...)  – slow part (≈1 s): reads the data once and records, per lead,
                             WHAT is true (facts, raw values, source row ids).
  2. apply_weights(...)    – fast part (≈50 ms): turns those signals into points using a
                             weights dict. Changing a weight never changes the evidence.

score_leads(...) = apply_weights(extract_signals(...), weights) and keeps the interface
agreed in plan.md:  one row per lead with components = [{component, points, fact,
source_table, source_ids}].
"""
import math
from copy import deepcopy

import pandas as pd

OPEN_STAGES = ["New", "Qualified", "Demo Scheduled", "Proposal Sent", "Negotiation"]
LATE_STAGES = ["Proposal Sent", "Negotiation"]
TARGET_INDUSTRIES = {"FinTech", "Healthcare", "E-commerce", "Logistics"}
ENGAGEMENT_WEIGHTS = {"email_open": 1, "email_click": 2, "pricing_page_visit": 4,
                      "call": 3, "meeting": 5, "demo_request": 6}
POSITIVE_KEYWORDS = ["budget approved", "pilot", "proposal", "renewal", "add seats",
                     "enterprise pricing", "strong buying signal", "actively evaluating", "champion"]
NEGATIVE_KEYWORDS = ["went with a competitor", "no response", "on hold", "not the right contact",
                     "budget is tight", "pricing concerns", "steep discount"]

ENGAGEMENT_WINDOW_DAYS = 30
NOTES_WINDOW_DAYS = 90
FIT_MIN_COMPANY_SIZE = 200

# ---------------------------------------------------------------------------
# Weights (what the manager can change). Defaults reproduce the original rubric exactly.
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "deal_size": 30,               # max pts for the largest open deal (log-scaled, $100k+ = max)
    "engagement": 30,              # max pts for engagement in the last 30 days
    "engagement_saturation": 20,   # weighted events needed to earn the full engagement points
    "urgency_30": 20,              # open deal expected to close within 30 days
    "urgency_60": 10,              # ... within 31-60 days
    "momentum": 5,                 # deal in Proposal Sent / Negotiation
    "fit_industry": 5,             # target industry
    "fit_size": 5,                 # company size >= 200
    "note_positive": 8,            # recent buying signal in call notes
    "note_negative": 10,           # recent objection in call notes (subtracted)
    "stale_penalty": 15,           # stale / unreachable record (subtracted)
    "no_contact_penalty": 5,       # no contact in 60+ days (subtracted)
    "event_weights": dict(ENGAGEMENT_WEIGHTS),
}

# label, help text – used by the "How scoring works" tab to build the sliders
WEIGHT_SPECS = {
    "deal_size": ("Deal size (max pts)", 0, 60, "Largest open deal, log-scaled: $1k ≈ 0, $100k+ = full points"),
    "engagement": ("Engagement, last 30 days (max pts)", 0, 60, "Weighted activity in the last 30 days"),
    "urgency_30": ("Close urgency: ≤ 30 days", 0, 40, "An open deal is expected to close within 30 days"),
    "urgency_60": ("Close urgency: 31–60 days", 0, 40, "An open deal is expected to close within 31–60 days"),
    "momentum": ("Deal momentum", 0, 20, "A deal is in Proposal Sent or Negotiation"),
    "fit_industry": ("ICP fit: target industry", 0, 20, "Industry is one of the target industries"),
    "fit_size": ("ICP fit: company size ≥ 200", 0, 20, "Company has at least 200 employees"),
    "note_positive": ("Call notes: buying signal (+)", 0, 30, "Most recent signal note in 90 days is positive"),
    "note_negative": ("Call notes: objection (−)", 0, 30, "Most recent signal note in 90 days is negative"),
    "stale_penalty": ("Stale / unreachable (−)", 0, 40, "No contact 180d+, missing, bounced or invalid email"),
    "no_contact_penalty": ("No contact in 60+ days (−)", 0, 20, "Not stale, but not contacted for 60+ days"),
}

PRESETS = {
    "Balanced (default)": {},
    "Close this quarter": {"deal_size": 25, "engagement": 20, "urgency_30": 35, "urgency_60": 20, "momentum": 15},
    "Engagement first": {"deal_size": 15, "engagement": 45, "urgency_30": 15, "urgency_60": 5, "note_positive": 12},
    "Big deals only": {"deal_size": 50, "engagement": 20, "urgency_30": 15, "urgency_60": 8, "fit_size": 10},
}


def resolve_weights(weights: dict | None = None) -> dict:
    """Fill any missing keys with defaults so partial dicts (e.g. presets) are valid."""
    w = deepcopy(DEFAULT_WEIGHTS)
    for k, v in (weights or {}).items():
        if k == "event_weights":
            w["event_weights"].update(v or {})
        elif k in w:
            w[k] = float(v)
    w["engagement_saturation"] = max(1.0, float(w["engagement_saturation"]))
    return w


def weights_changed(weights: dict | None) -> dict:
    """Return only the weights that differ from the defaults ({} if none)."""
    w, d = resolve_weights(weights), DEFAULT_WEIGHTS
    out = {k: w[k] for k in d if k != "event_weights" and float(w[k]) != float(d[k])}
    ev = {k: v for k, v in w["event_weights"].items() if float(v) != float(d["event_weights"].get(k, 1))}
    if ev:
        out["event_weights"] = ev
    return out


def rubric(weights: dict | None = None) -> list[tuple[str, str]]:
    w = resolve_weights(weights)
    ew = w["event_weights"]
    f = lambda x: f"{x:g}"
    return [
        ("Deal size", f"up to {f(w['deal_size'])} pts – largest open deal, log-scaled ($1k≈0, $100k+ = max)"),
        ("Engagement (last 30 days)", f"up to {f(w['engagement'])} pts – full points at {f(w['engagement_saturation'])} weighted events: "
                                      + ", ".join(f"{k.replace('_', ' ')} {f(v)}" for k, v in sorted(ew.items(), key=lambda kv: -kv[1]))),
        ("Close urgency", f"{f(w['urgency_30'])} pts if an open deal is expected to close within 30 days, {f(w['urgency_60'])} pts within 60 days"),
        ("Deal momentum", f"{f(w['momentum'])} pts if a deal is in Proposal Sent / Negotiation"),
        ("Ideal customer fit", f"target industry ({f(w['fit_industry'])}) + company size ≥ {FIT_MIN_COMPANY_SIZE} ({f(w['fit_size'])})"),
        ("Call-note signals", f"+{f(w['note_positive'])} for a recent positive buying signal, −{f(w['note_negative'])} for a negative signal (last 90 days)"),
        ("Data quality / recency", f"−{f(w['stale_penalty'])} if stale (no contact 180d+ / missing or bad email), −{f(w['no_contact_penalty'])} if no contact in 60d+"),
    ]


RUBRIC = rubric()   # backwards compatible constant (default weights)


def _ids(df: pd.DataFrame, col: str) -> list[str]:
    """ALL row ids behind a component – every point must be traceable, so nothing is truncated."""
    return df[col].astype(str).tolist()


# ---------------------------------------------------------------------------
# Step 1: signals (facts + evidence, no points)
# ---------------------------------------------------------------------------
def extract_signals(leads: pd.DataFrame, deals: pd.DataFrame, activity: pd.DataFrame,
                    notes: pd.DataFrame, ref_date: pd.Timestamp) -> list[dict]:
    open_deals = deals[deals["stage"].isin(OPEN_STAGES)]
    recent_act = activity[activity["activity_date"] >= ref_date - pd.Timedelta(days=ENGAGEMENT_WINDOW_DAYS)]
    recent_notes = notes[notes["note_date"] >= ref_date - pd.Timedelta(days=NOTES_WINDOW_DAYS)]

    od = dict(tuple(open_deals.groupby("lead_id")))
    ra = dict(tuple(recent_act.groupby("lead_id")))
    rn = dict(tuple(recent_notes.groupby("lead_id")))

    out = []
    for lead in leads.itertuples(index=False):
        lid = lead.lead_id
        sig = []
        top_deal = None
        close_in_days = None
        note_signal = 0

        # 1. Deal size  + 3. urgency + 4. momentum
        d = od.get(lid)
        if d is not None and len(d):
            top_deal = d.sort_values(["amount_usd", "deal_id"], ascending=[False, True]).iloc[0]
            amt = float(top_deal["amount_usd"])
            sig.append({"key": "deal_size", "component": "Deal size",
                        "value": max(0.0, min(1.0, 0.5 * math.log10(max(amt, 1) / 1000))),
                        "fact": f"Open deal {top_deal['deal_id']} '{top_deal['deal_name']}' worth ${amt:,.0f} ({top_deal['stage']})",
                        "source_table": "deals", "source_ids": _ids(d.sort_values("amount_usd", ascending=False), "deal_id")})

            dtc_all = (d["expected_close_date"] - ref_date).dt.days
            soon = d[dtc_all.between(0, 60)]
            if len(soon):
                nearest = soon.sort_values(["expected_close_date", "deal_id"]).iloc[0]
                dtc = int((nearest["expected_close_date"] - ref_date).days)
                close_in_days = dtc
                sig.append({"key": "urgency_30" if dtc <= 30 else "urgency_60", "component": "Close urgency", "value": 1.0,
                            "fact": f"Deal {nearest['deal_id']} expected to close in {dtc} days ({nearest['expected_close_date'].date()})",
                            "source_table": "deals", "source_ids": [str(nearest["deal_id"])]})
            else:
                days_to_close = (top_deal["expected_close_date"] - ref_date).days
                if pd.notna(days_to_close) and days_to_close < 0:
                    sig.append({"key": "info", "component": "Close urgency", "value": 0.0,
                                "fact": f"Deal {top_deal['deal_id']} is past its expected close date by {-int(days_to_close)} days (slipping)",
                                "source_table": "deals", "source_ids": [str(top_deal["deal_id"])]})
            late = d[d["stage"].isin(LATE_STAGES)]
            if len(late):
                sig.append({"key": "momentum", "component": "Deal momentum", "value": 1.0,
                            "fact": f"{len(late)} deal(s) in late stage: {', '.join(late['stage'].unique())}",
                            "source_table": "deals", "source_ids": _ids(late, "deal_id")})

        # 2. Engagement (raw counts; points depend on the event weights)
        a = ra.get(lid)
        if a is not None and len(a):
            vc = a["type"].value_counts()
            counts = pd.Series(dict(sorted(vc.items(), key=lambda kv: (-kv[1], kv[0]))))   # ties in a fixed order
            desc = ", ".join(f"{v}× {k.replace('_', ' ')}" for k, v in counts.items())
            sig.append({"key": "engagement", "component": "Engagement (last 30 days)", "value": counts.to_dict(),
                        "fact": f"{len(a)} interactions in the last 30 days: {desc}",
                        "source_table": "activity",
                        "source_ids": _ids(a.sort_values(["activity_date", "activity_id"], ascending=False), "activity_id")})

        # 5. Fit
        fit = {}
        if lead.industry in TARGET_INDUSTRIES:
            fit["fit_industry"] = f"target industry ({lead.industry})"
        if lead.company_size >= FIT_MIN_COMPANY_SIZE:
            fit["fit_size"] = f"{lead.company_size:,} employees"
        if fit:
            sig.append({"key": "fit", "component": "Ideal customer fit", "value": fit,
                        "fact": "", "source_table": "leads", "source_ids": [lid]})

        # 6. Notes signals: the most recent note that carries a signal decides
        n = rn.get(lid)
        if n is not None and len(n):
            for note in n.sort_values(["note_date", "note_id"], ascending=False).itertuples(index=False):
                t = str(note.text).lower()
                if any(k in t for k in NEGATIVE_KEYWORDS):
                    note_signal = -1
                    sig.append({"key": "note_negative", "component": "Call-note signals", "value": -1.0,
                                "fact": f"Negative signal on {note.note_date.date()}: \"{note.text}\"",
                                "source_table": "notes", "source_ids": [note.note_id]})
                    break
                if any(k in t for k in POSITIVE_KEYWORDS):
                    note_signal = 1
                    sig.append({"key": "note_positive", "component": "Call-note signals", "value": 1.0,
                                "fact": f"Buying signal on {note.note_date.date()}: \"{note.text}\"",
                                "source_table": "notes", "source_ids": [note.note_id]})
                    break

        # 7. Data quality / recency
        dsc = lead.days_since_contact
        contact_known = pd.notna(dsc)   # an uploaded CRM can have leads with no (readable) contact date
        if lead.is_stale:
            sig.append({"key": "stale_penalty", "component": "Data quality / recency", "value": -1.0,
                        "fact": f"Stale record: {lead.stale_reason}", "source_table": "leads", "source_ids": [lid]})
        elif not contact_known:         # unknown is NOT the same as recent: same penalty as 60+ days
            sig.append({"key": "no_contact_penalty", "component": "Data quality / recency", "value": -1.0,
                        "fact": "No last-contact date on record", "source_table": "leads", "source_ids": [lid]})
        elif dsc > 60:
            sig.append({"key": "no_contact_penalty", "component": "Data quality / recency", "value": -1.0,
                        "fact": f"No contact in {int(lead.days_since_contact)} days", "source_table": "leads", "source_ids": [lid]})

        out.append({
            "lead_id": lid, "name": lead.name, "company": lead.company, "title": lead.title,
            "email": lead.email, "industry": lead.industry, "company_size": lead.company_size,
            "days_since_contact": int(dsc) if contact_known else None, "is_stale": bool(lead.is_stale),
            "stale_reason": getattr(lead, "stale_reason", ""),
            "top_deal_id": top_deal["deal_id"] if top_deal is not None else None,
            "top_deal_amount": float(top_deal["amount_usd"]) if top_deal is not None else 0.0,
            "top_deal_stage": top_deal["stage"] if top_deal is not None else None,
            "close_in_days": close_in_days, "note_signal": note_signal,
            "merged_from": lead.merged_from, "signals": sig,
        })
    return out


# ---------------------------------------------------------------------------
# Step 2: weights -> points (fast, run on every slider change)
# ---------------------------------------------------------------------------
def _points(s: dict, w: dict) -> tuple[float, str]:
    k = s["key"]
    if k == "info":
        return 0.0, s["fact"]
    if k == "deal_size":
        return s["value"] * w["deal_size"], s["fact"]
    if k == "engagement":
        raw = sum(w["event_weights"].get(t, 1) * n for t, n in s["value"].items())
        return w["engagement"] * min(1.0, raw / w["engagement_saturation"]), s["fact"]
    if k == "fit":
        parts = {p: why for p, why in s["value"].items() if w[p]}
        return sum(w[p] for p in parts), ("Fits ICP: " + ", ".join(parts.values())) if parts else ""
    # flags: urgency_30/60, momentum (+), note_positive (+), note_negative / penalties (value −1)
    return s["value"] * w[k], s["fact"]


def apply_weights(signals: list[dict], weights: dict | None = None) -> pd.DataFrame:
    w = resolve_weights(weights)
    rows = []
    for lead in signals:
        comps = []
        for s in lead["signals"]:
            pts, fact = _points(s, w)
            if s["key"] != "info" and abs(pts) < 1e-9:
                continue   # weight switched off (or no value) -> not part of the score
            comps.append({"component": s["component"], "points": round(pts, 1), "fact": fact,
                          "source_table": s["source_table"], "source_ids": list(s["source_ids"])})
        row = {k: v for k, v in lead.items() if k != "signals"}
        row["score"] = round(sum(c["points"] for c in comps), 1)
        row["components"] = comps
        rows.append(row)
    out = pd.DataFrame(rows)
    out = out.sort_values(["score", "lead_id"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    out["rank"] = out.index + 1
    return out


def score_leads(leads: pd.DataFrame, deals: pd.DataFrame, activity: pd.DataFrame,
                notes: pd.DataFrame, ref_date: pd.Timestamp, weights: dict | None = None) -> pd.DataFrame:
    """Return one row per lead with total score, component breakdown and evidence."""
    return apply_weights(extract_signals(leads, deals, activity, notes, ref_date), weights)


def suggest_action(row: dict) -> str:
    """Rule-based next-best-action for a lead (the human approves it).
    Uses the underlying facts, not the points, so it stays correct when weights change."""
    comps = {c["component"]: c for c in row["components"]}
    if row.get("is_stale") and any(x in str(row.get("stale_reason", "")) for x in ("missing email", "bounced", "invalid email")):
        return "Find a working email or phone number before any outreach"
    if row.get("note_signal", 0) < 0:
        return "Send a re-engagement email addressing their concern"
    cid = row.get("close_in_days")
    if cid is not None and not pd.isna(cid) and cid <= 30:
        return "Call today to push the deal over the line before close date"
    if row.get("top_deal_stage") in LATE_STAGES:
        return "Schedule a negotiation call with the decision maker"
    eng = comps.get("Engagement (last 30 days)")
    if eng and ("pricing page" in eng["fact"] or "demo request" in eng["fact"]):
        return "Book a demo / pricing walkthrough"
    if row.get("top_deal_id"):
        return "Send a follow-up email with a tailored case study"
    return "Send an intro email and qualify the lead"
