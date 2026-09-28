"""
Decision agent – transparent lead scoring.

The SCORE is computed by explicit, auditable rules (never by the LLM).
Every component carries the evidence (row ids) that produced it, so the UI can
show a "Why?" panel that traces back to the data. The LLM is only used later to
turn these facts into a readable sentence.
"""
import math

import pandas as pd

OPEN_STAGES = ["New", "Qualified", "Demo Scheduled", "Proposal Sent", "Negotiation"]
TARGET_INDUSTRIES = {"FinTech", "Healthcare", "E-commerce", "Logistics"}
ENGAGEMENT_WEIGHTS = {"email_open": 1, "email_click": 2, "pricing_page_visit": 4,
                      "call": 3, "meeting": 5, "demo_request": 6}
POSITIVE_KEYWORDS = ["budget approved", "pilot", "proposal", "renewal", "add seats",
                     "enterprise pricing", "strong buying signal", "actively evaluating", "champion"]
NEGATIVE_KEYWORDS = ["went with a competitor", "no response", "on hold", "not the right contact",
                     "budget is tight", "pricing concerns", "steep discount"]

RUBRIC = [
    ("Deal size", "up to 30 pts – largest open deal, log-scaled ($1k≈0, $100k+≈30)"),
    ("Engagement (last 30 days)", "up to 30 pts – weighted events: demo request 6, meeting 5, pricing visit 4, call 3, click 2, open 1"),
    ("Close urgency", "20 pts if an open deal is expected to close within 30 days, 10 pts within 60 days"),
    ("Deal momentum", "5 pts if the deal is in Proposal Sent / Negotiation"),
    ("Ideal customer fit", "up to 10 pts – target industry (5) + company size ≥ 200 (5)"),
    ("Call-note signals", "+8 for a recent positive buying signal, −10 for a negative signal (last 90 days)"),
    ("Data quality / recency", "−15 if stale (no contact 180d+ / bad email), −5 if no contact in 60d+"),
]


def _ids(df: pd.DataFrame, col: str, limit: int = 8) -> list[str]:
    return df[col].astype(str).head(limit).tolist()


def score_leads(leads: pd.DataFrame, deals: pd.DataFrame, activity: pd.DataFrame,
                notes: pd.DataFrame, ref_date: pd.Timestamp) -> pd.DataFrame:
    """Return one row per lead with total score, component breakdown and evidence."""
    open_deals = deals[deals["stage"].isin(OPEN_STAGES)]
    recent_act = activity[activity["activity_date"] >= ref_date - pd.Timedelta(days=30)]
    recent_notes = notes[notes["note_date"] >= ref_date - pd.Timedelta(days=90)]

    od = dict(tuple(open_deals.groupby("lead_id")))
    ra = dict(tuple(recent_act.groupby("lead_id")))
    rn = dict(tuple(recent_notes.groupby("lead_id")))

    rows = []
    for lead in leads.itertuples(index=False):
        comps = []
        lid = lead.lead_id

        # 1. Deal size
        d = od.get(lid)
        top_deal = None
        if d is not None and len(d):
            top_deal = d.sort_values("amount_usd", ascending=False).iloc[0]
            amt = float(top_deal["amount_usd"])
            pts = max(0.0, min(30.0, 15 * math.log10(max(amt, 1) / 1000)))
            comps.append({"component": "Deal size", "points": round(pts, 1),
                          "fact": f"Open deal {top_deal['deal_id']} '{top_deal['deal_name']}' worth ${amt:,.0f} ({top_deal['stage']})",
                          "source_table": "deals", "source_ids": _ids(d, "deal_id")})

            # 3. urgency + 4. momentum
            days_to_close = (top_deal["expected_close_date"] - ref_date).days
            soon = d[(d["expected_close_date"] - ref_date).dt.days.between(0, 60)]
            if len(soon):
                nearest = soon.sort_values("expected_close_date").iloc[0]
                dtc = (nearest["expected_close_date"] - ref_date).days
                pts = 20 if dtc <= 30 else 10
                comps.append({"component": "Close urgency", "points": pts,
                              "fact": f"Deal {nearest['deal_id']} expected to close in {dtc} days ({nearest['expected_close_date'].date()})",
                              "source_table": "deals", "source_ids": [nearest["deal_id"]]})
            elif days_to_close < 0:
                comps.append({"component": "Close urgency", "points": 0,
                              "fact": f"Deal {top_deal['deal_id']} is past its expected close date by {-days_to_close} days (slipping)",
                              "source_table": "deals", "source_ids": [top_deal["deal_id"]]})
            late = d[d["stage"].isin(["Proposal Sent", "Negotiation"])]
            if len(late):
                comps.append({"component": "Deal momentum", "points": 5,
                              "fact": f"{len(late)} deal(s) in late stage: {', '.join(late['stage'].unique())}",
                              "source_table": "deals", "source_ids": _ids(late, "deal_id")})

        # 2. Engagement
        a = ra.get(lid)
        if a is not None and len(a):
            raw = sum(ENGAGEMENT_WEIGHTS.get(t, 1) for t in a["type"])
            pts = min(30.0, raw * 1.5)
            counts = a["type"].value_counts()
            desc = ", ".join(f"{v}× {k.replace('_', ' ')}" for k, v in counts.items())
            comps.append({"component": "Engagement (last 30 days)", "points": round(pts, 1),
                          "fact": f"{len(a)} interactions in the last 30 days: {desc}",
                          "source_table": "activity", "source_ids": _ids(a.sort_values("activity_date", ascending=False), "activity_id")})

        # 5. Fit
        fit = 0
        why = []
        if lead.industry in TARGET_INDUSTRIES:
            fit += 5
            why.append(f"target industry ({lead.industry})")
        if lead.company_size >= 200:
            fit += 5
            why.append(f"{lead.company_size} employees")
        if fit:
            comps.append({"component": "Ideal customer fit", "points": fit, "fact": "Fits ICP: " + ", ".join(why),
                          "source_table": "leads", "source_ids": [lid]})

        # 6. Notes signals
        n = rn.get(lid)
        if n is not None and len(n):
            n = n.sort_values("note_date", ascending=False)
            for note in n.itertuples(index=False):
                t = note.text.lower()
                if any(k in t for k in NEGATIVE_KEYWORDS):
                    comps.append({"component": "Call-note signals", "points": -10,
                                  "fact": f"Negative signal on {note.note_date.date()}: \"{note.text}\"",
                                  "source_table": "notes", "source_ids": [note.note_id]})
                    break
                if any(k in t for k in POSITIVE_KEYWORDS):
                    comps.append({"component": "Call-note signals", "points": 8,
                                  "fact": f"Buying signal on {note.note_date.date()}: \"{note.text}\"",
                                  "source_table": "notes", "source_ids": [note.note_id]})
                    break

        # 7. Data quality / recency
        if lead.is_stale:
            comps.append({"component": "Data quality / recency", "points": -15, "fact": f"Stale record: {lead.stale_reason}",
                          "source_table": "leads", "source_ids": [lid]})
        elif lead.days_since_contact > 60:
            comps.append({"component": "Data quality / recency", "points": -5,
                          "fact": f"No contact in {int(lead.days_since_contact)} days",
                          "source_table": "leads", "source_ids": [lid]})

        total = round(sum(c["points"] for c in comps), 1)
        rows.append({
            "lead_id": lid, "name": lead.name, "company": lead.company, "title": lead.title,
            "email": lead.email, "industry": lead.industry, "company_size": lead.company_size,
            "days_since_contact": int(lead.days_since_contact), "is_stale": bool(lead.is_stale),
            "top_deal_id": top_deal["deal_id"] if top_deal is not None else None,
            "top_deal_amount": float(top_deal["amount_usd"]) if top_deal is not None else 0.0,
            "top_deal_stage": top_deal["stage"] if top_deal is not None else None,
            "score": total, "components": comps,
            "merged_from": lead.merged_from,
        })

    out = pd.DataFrame(rows).sort_values("score", ascending=False).reset_index(drop=True)
    out["rank"] = out.index + 1
    return out


def suggest_action(row: dict) -> str:
    """Rule-based next-best-action for a lead (the human approves it)."""
    comps = {c["component"]: c for c in row["components"]}
    notes = comps.get("Call-note signals", {})
    if notes.get("points", 0) < 0:
        return "Send a re-engagement email addressing their concern"
    if "Close urgency" in comps and comps["Close urgency"]["points"] >= 20:
        return "Call today to push the deal over the line before close date"
    if row.get("top_deal_stage") in ("Proposal Sent", "Negotiation"):
        return "Schedule a negotiation call with the decision maker"
    eng = comps.get("Engagement (last 30 days)")
    if eng and ("pricing page" in eng["fact"] or "demo request" in eng["fact"]):
        return "Book a demo / pricing walkthrough"
    if row.get("top_deal_id"):
        return "Send a follow-up email with a tailored case study"
    return "Send an intro email and qualify the lead"
