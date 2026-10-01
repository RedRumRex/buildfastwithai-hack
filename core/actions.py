"""
Explanations, outreach drafts and the human-approval audit trail.
The LLM only writes words here; it never changes a score or takes an action on its own.
Every id the LLM cites is checked against the ids it was actually given (core/citations.py).
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

from . import llm
from .citations import check_citations, prefixes_of

AUDIT_FILE = Path(os.getenv("LEADLENS_AUDIT_FILE") or Path(__file__).resolve().parent.parent / "data" / "audit_log.csv")
# decision is one of: approved · rejected · edited (email draft changed) · undone
AUDIT_FIELDS = ["timestamp", "reviewer", "lead_id", "name", "company", "score", "decision",
                "action", "reviewer_note", "evidence_ids", "email_subject", "email_body", "weights", "details"]
ENC = "utf-8"


def _clean_ws(s: str) -> str:
    return str(s).replace("\u202f", " ").replace("\u00a0", " ")


def evidence_ids(components: list[dict]) -> list[str]:
    ids = []
    for c in components:
        ids.extend(c["source_ids"][:3])
    return list(dict.fromkeys(ids))


def template_explanation(row: dict) -> str:
    comps = sorted(row["components"], key=lambda c: -c["points"])
    pos = [c for c in comps if c["points"] > 0][:3]
    neg = [c for c in comps if c["points"] < 0]
    txt = "; ".join(c["fact"] for c in pos) or "No strong signals"
    if neg:
        txt += ". Caution: " + "; ".join(c["fact"] for c in neg)
    return txt + "."


def explain_checked(row: dict) -> tuple[str, dict]:
    """Explanation + citation report. Allowed ids = exactly the ids shown to the LLM (+ the lead id)."""
    allowed = set(evidence_ids(row["components"])) | {str(row["lead_id"])}
    empty = {"cited": [], "valid": [], "invalid": [], "engine": "template"}
    if not llm.available():
        return template_explanation(row), empty
    facts = "\n".join(f"- ({c['points']:+} pts) {c['fact']} [source: {c['source_table']} {', '.join(c['source_ids'][:3])}]"
                      for c in row["components"])
    try:
        txt = llm.chat(
            "You explain to a sales rep why a lead is prioritised. Use ONLY the facts given. "
            "Max 2 sentences, plain language, mention the most important numbers. "
            "Cite ids in square brackets exactly as written in the facts, e.g. [D00123]. Never invent an id.",
            f"Lead: {row['name']} ({row.get('title', '')}) at {row['company']}. Priority score {row['score']}.\nFacts:\n{facts}",
            max_tokens=160)
    except Exception:
        return template_explanation(row), empty
    pre = prefixes_of(allowed) | {"L", "D", "A", "N"}
    txt, rep = check_citations(_clean_ws(txt), allowed, mode="drop", prefixes=pre)
    rep["engine"] = llm.model_name()
    return txt, rep


def explain(row: dict) -> str:
    """One-to-two sentence reason for prioritising this lead, grounded in the scored facts."""
    return explain_checked(row)[0]


def draft_email(row: dict, action: str, sender: str = "Your Account Executive") -> tuple[str, str]:
    first = str(row["name"]).replace("Mrs ", "").replace("Mr ", "").replace("Dr. ", "").split()[0].title()
    facts = "\n".join(f"- {c['fact']}" for c in row["components"] if c["points"] > 0)
    if llm.available():
        try:
            out = llm.chat_json(
                "Write a short, friendly B2B sales email (under 120 words). Use ONLY the facts provided; do not invent "
                "discounts, dates or features. Don't mention internal scores, record ids or tracking (e.g. 'you opened our email').",
                f"Recipient: {row['name']}, {row.get('title', '')} at {row['company']}\nGoal: {action}\nFacts:\n{facts}\n"
                f"Sender: {sender}\nReturn {{\"subject\": \"...\", \"body\": \"...\"}}")
            pre = prefixes_of(evidence_ids(row["components"])) | {"L", "D", "A", "N"}
            subject, _ = check_citations(_clean_ws(out["subject"]), set(), mode="strip", prefixes=pre)
            body, _ = check_citations(_clean_ws(out["body"]), set(), mode="strip", prefixes=pre)
            if subject and body:
                return subject, body
        except Exception:
            pass
    a = action.lower()
    if "concern" in a or "re-engage" in a:
        subject = f"Picking up our conversation, {row['company']}"
        middle = ("I know timing and budget were on your mind when we last spoke. I'd love to understand what has changed "
                  "and share a few options that might fit better.")
    elif "demo" in a or "pricing" in a:
        subject = f"A walkthrough tailored to {row['company']}"
        middle = "I'd be glad to set up a short walkthrough of pricing and the features most relevant to your team."
    elif "negotiation" in a or "close" in a or "push" in a:
        subject = f"Next steps on the {row['company']} proposal"
        middle = "I wanted to check whether there's anything left to clarify on the proposal so we can agree on next steps."
    elif "intro" in a:
        subject = f"Quick introduction for {row['company']}"
        middle = f"I work with {row['industry']} teams like yours and thought a short conversation could be useful."
    else:
        subject = f"Following up, {row['company']}"
        middle = "I wanted to follow up and share how similar teams have been getting value from our platform."
    body = (f"Hi {first},\n\nThanks for your time recently. {middle}\n\n"
            f"Would you have 20 minutes this week for a quick call?\n\nBest regards,\n{sender}")
    return subject, body


def _migrate_header():
    """Older logs had fewer columns – rewrite them once with the current header so rows stay aligned."""
    if not AUDIT_FILE.exists() or AUDIT_FILE.stat().st_size == 0:
        return
    with AUDIT_FILE.open(newline="", encoding=ENC, errors="replace") as f:
        header = next(csv.reader(f), [])
    if header == AUDIT_FIELDS:
        return
    with AUDIT_FILE.open(newline="", encoding=ENC, errors="replace") as f:
        rows = list(csv.DictReader(f))
    with AUDIT_FILE.open("w", newline="", encoding=ENC) as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def log_decision(row: dict, decision: str, action: str, reviewer: str, note: str = "", subject: str = "",
                 body: str = "", details: str = "", weights: str = "default") -> dict:
    """Append one human decision to the audit log. Every approve / reject / edit / undo is recorded
    with the evidence ids behind the lead's score and the weight profile that produced the ranking."""
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "reviewer": reviewer, "lead_id": row["lead_id"], "name": row["name"], "company": row["company"],
        "score": row["score"], "decision": decision, "action": action, "reviewer_note": note,
        "evidence_ids": json.dumps(evidence_ids(row["components"])), "email_subject": subject,
        "email_body": body, "weights": weights, "details": details,
    }
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    _migrate_header()
    new = not AUDIT_FILE.exists() or AUDIT_FILE.stat().st_size == 0
    with AUDIT_FILE.open("a", newline="", encoding=ENC) as f:
        w = csv.DictWriter(f, fieldnames=AUDIT_FIELDS)
        if new:
            w.writeheader()
        w.writerow(entry)
    return entry


def read_audit() -> list[dict]:
    if not AUDIT_FILE.exists() or AUDIT_FILE.stat().st_size == 0:
        return []
    _migrate_header()
    with AUDIT_FILE.open(newline="", encoding=ENC, errors="replace") as f:
        return list(csv.DictReader(f))