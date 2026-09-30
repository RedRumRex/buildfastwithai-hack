"""
Generate a realistic, deliberately MESSY synthetic CRM for a B2B SaaS sales team.

Outputs (in this folder):
  leads.csv     - contacts/leads (with injected duplicates, missing & bounced emails, stale records)
  deals.csv     - pipeline deals linked to leads
  activity.csv  - engagement events (email opens, pricing-page visits, calls, meetings, demo requests)
  notes.csv     - free-text sales call notes (used for RAG search)
  truth_duplicates.csv - ground truth for the dedupe evaluation (duplicate_id, original_id, mangle_type)

Messiness injected on purpose (every duplicate is recorded in truth_duplicates.csv):
  - 7% duplicates re-typed with a different company spelling, name spelling, upper-case or blank email
  - 3% "hard" duplicates: company renamed (email on the new domain), email typos (some make the email
    invalid), conflicting titles and phone numbers
  - missing and bounced emails, invalid email formats, missing phone numbers, stale records

Run:  python data/generate_data.py            (default 2000 leads, seed 42, "today" = 2026-09-26)
      python data/generate_data.py --leads 5000
      python data/generate_data.py --today 2026-10-01

The output is fully determined by --leads, --seed and --today, so the committed sample data can be
reproduced exactly. After regenerating, re-run data/plant_hot_leads.py and eval/eval_ranking.py.
"""
import argparse
import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

OUT = Path(__file__).parent
DEFAULT_TODAY = "2026-09-26"   # the date the committed sample data was generated with
TODAY = date.fromisoformat(DEFAULT_TODAY)

INDUSTRIES = ["FinTech", "Healthcare", "E-commerce", "Manufacturing", "EdTech",
              "Logistics", "Retail", "Media", "Real Estate", "Telecom"]
SIZES = [10, 25, 50, 120, 250, 500, 1200, 3000, 8000]
TITLES = ["Head of Sales", "VP Operations", "CTO", "Founder", "Procurement Manager",
          "IT Director", "Growth Lead", "COO", "Engineering Manager", "CFO"]
SOURCES = ["Website", "Webinar", "Referral", "LinkedIn Ads", "Trade Show", "Cold Outbound"]
COUNTRIES = ["India", "USA", "UK", "Germany", "Singapore", "UAE", "Australia"]
STAGES = ["New", "Qualified", "Demo Scheduled", "Proposal Sent", "Negotiation", "Closed Won", "Closed Lost"]
SUFFIXES = ["Inc.", "Corp", "Ltd", "LLC", "Pvt Ltd", "Technologies", "Solutions", "Group"]

POSITIVE_NOTES = [
    "Budget approved for this quarter, wants a proposal by end of month.",
    "Very interested after the demo, asked about onboarding timelines.",
    "Champion is pushing internally, needs a security questionnaire filled.",
    "Current contract with competitor ends soon, actively evaluating us.",
    "Asked for enterprise pricing and SSO details, strong buying signal.",
    "Decision maker joined the call, wants to start a pilot next month.",
    "Renewal coming up, happy with the product and wants to add seats.",
]
NEUTRAL_NOTES = [
    "Intro call went fine, will circle back after their planning cycle.",
    "Requested case studies from their industry.",
    "Evaluating three vendors, we are on the shortlist.",
    "Wants to see integration with their existing ERP.",
    "Asked for a follow-up call with their technical team.",
]
NEGATIVE_NOTES = [
    "Client worried about price, says budget is tight this year.",
    "Went with a competitor, might revisit next year.",
    "No response after three follow-ups.",
    "Pricing concerns raised again, asked for a steep discount.",
    "Project put on hold due to internal reorganisation.",
    "Not the right contact, gatekeeper refused to share the decision maker.",
]


def mangle_company(name: str, rng=random) -> tuple[str, str]:
    """Create a plausible duplicate spelling of a company name. Returns (value, mangle type)."""
    base = name
    for s in SUFFIXES:
        base = base.replace(" " + s, "")
    choice = rng.random()
    if choice < 0.3:
        return base.upper() + " " + rng.choice(SUFFIXES), "company_upper_new_suffix"
    if choice < 0.6:
        return base + " " + rng.choice(SUFFIXES), "company_new_suffix"
    if choice < 0.8:
        return base.lower(), "company_lower_no_suffix"
    return base.replace(" ", "") + " " + rng.choice(SUFFIXES), "company_nospace_new_suffix"


def mangle_name(name: str, rng=random) -> tuple[str, str]:
    """Create a plausible duplicate spelling of a person's name. Returns (value, mangle type)."""
    parts = name.split()
    r = rng.random()
    if r < 0.4 and len(parts) >= 2:
        return f"{parts[0][0]}. {parts[-1]}", "name_initial"   # "P. Sharma"
    if r < 0.7:
        return name.upper(), "name_upper"
    return name + " ", "name_trailing_space"


def typo_email(email: str, rng, invalid: bool = False) -> tuple[str, str]:
    """A mistyped copy of an email address. Returns (value, mangle type)."""
    local, domain = email.split("@")
    if invalid or rng.random() < 0.3:     # typos that also break the format
        r = rng.random()
        if r < 0.35:
            return local + domain, "email_typo_invalid"                          # '@' dropped
        if r < 0.7:
            return f"{local}@{domain.rsplit('.', 1)[0]}", "email_typo_invalid"   # '.com' dropped
        return f"{local}@@{domain}", "email_typo_invalid"
    i = rng.randrange(len(local) - 1)
    r = rng.random()
    if r < 0.4:                           # two letters swapped
        local = local[:i] + local[i + 1] + local[i] + local[i + 2:]
    elif r < 0.7:                         # a letter dropped
        local = local[:i] + local[i + 1:]
    else:                                 # a letter doubled
        local = local[:i] + local[i] + local[i:]
    return f"{local}@{domain}", "email_typo"


def main(n_leads: int, seed: int):
    random.seed(seed)
    fake = Faker(["en_US", "en_IN", "en_GB"])
    Faker.seed(seed)

    leads, deals, activity, notes = [], [], [], []
    companies = [f"{fake.last_name()} {random.choice(SUFFIXES)}" for _ in range(int(n_leads * 0.7))]

    # ---- 1. clean base leads -------------------------------------------------
    for i in range(n_leads):
        lid = f"L{i+1:05d}"
        name = fake.name()
        company = random.choice(companies)
        first = name.split()[0].lower().strip(".")
        last = name.split()[-1].lower().strip(".")
        domain = company.split()[0].lower() + ".com"
        email = f"{first}.{last}@{domain}"
        created = TODAY - timedelta(days=random.randint(20, 720))
        # a chunk of leads have not been touched in a long time (stale)
        if random.random() < 0.2:
            last_contact = created + timedelta(days=random.randint(0, 60))
            last_contact = min(last_contact, TODAY - timedelta(days=random.randint(181, 400)))
        else:
            last_contact = TODAY - timedelta(days=random.randint(0, 120))
        last_contact = max(last_contact, created)
        leads.append({
            "lead_id": lid,
            "name": name,
            "company": company,
            "email": email,
            "phone": fake.phone_number(),
            "title": random.choice(TITLES),
            "industry": random.choice(INDUSTRIES),
            "company_size": random.choice(SIZES),
            "country": random.choice(COUNTRIES),
            "source": random.choice(SOURCES),
            "created_at": created.isoformat(),
            "last_contact_date": last_contact.isoformat(),
            "email_status": "valid",
        })

    # messiness: missing / bounced emails
    for lead in random.sample(leads, int(n_leads * 0.05)):
        lead["email"] = ""
    for lead in random.sample(leads, int(n_leads * 0.04)):
        if lead["email"]:
            lead["email_status"] = "bounced"

    # ---- 2. deals ------------------------------------------------------------
    d = 0
    for lead in leads:
        if random.random() < 0.55:
            for _ in range(random.choice([1, 1, 1, 2])):
                d += 1
                stage = random.choices(STAGES, weights=[18, 18, 14, 14, 12, 12, 12])[0]
                close_offset = random.randint(-60, 120) if stage not in ("Closed Won", "Closed Lost") else random.randint(-200, -1)
                deals.append({
                    "deal_id": f"D{d:05d}",
                    "lead_id": lead["lead_id"],
                    "deal_name": f"{lead['company'].split()[0]} - {random.choice(['Platform', 'Pilot', 'Expansion', 'Renewal', 'Enterprise'])}",
                    "amount_usd": int(random.lognormvariate(9.8, 0.9)) // 100 * 100,
                    "stage": stage,
                    "expected_close_date": (TODAY + timedelta(days=close_offset)).isoformat(),
                    "last_stage_change": (TODAY - timedelta(days=random.randint(1, 150))).isoformat(),
                })

    # ---- 3. activity (engagement) ---------------------------------------------
    types = ["email_open", "email_click", "pricing_page_visit", "call", "meeting", "demo_request"]
    weights = [45, 18, 12, 12, 8, 5]
    a = 0
    for lead in leads:
        hot = random.random() < 0.15
        n = random.randint(4, 14) if hot else random.randint(0, 5)
        stale = (TODAY - date.fromisoformat(lead["last_contact_date"])).days > 180
        for _ in range(n):
            a += 1
            lo = 0 if (hot and not stale) else 20
            hi = 30 if hot and not stale else 365
            activity.append({
                "activity_id": f"A{a:06d}",
                "lead_id": lead["lead_id"],
                "type": random.choices(types, weights=weights)[0],
                "activity_date": (TODAY - timedelta(days=random.randint(lo, max(lo + 1, hi)))).isoformat(),
            })

    # ---- 4. notes ---------------------------------------------------------------
    nid = 0
    for lead in leads:
        if random.random() < 0.45:
            for _ in range(random.choice([1, 1, 2])):
                nid += 1
                pool = random.choices([POSITIVE_NOTES, NEUTRAL_NOTES, NEGATIVE_NOTES], weights=[3, 4, 3])[0]
                notes.append({
                    "note_id": f"N{nid:05d}",
                    "lead_id": lead["lead_id"],
                    "note_date": (TODAY - timedelta(days=random.randint(0, 200))).isoformat(),
                    "author": random.choice(["Aarav (AE)", "Meera (SDR)", "Rohan (AE)", "Sara (SDR)"]),
                    "text": random.choice(pool),
                })

    # ---- 5. inject DUPLICATES (same person, entered again with messy spelling) ----
    n_dups = int(n_leads * 0.07)
    next_id = n_leads
    truth_dups = []
    for src in random.sample(leads, n_dups):
        next_id += 1
        dup_id = f"L{next_id:05d}"
        dup = dict(src)
        dup["lead_id"] = dup_id
        dup["company"], company_mangle = mangle_company(src["company"])
        dup["name"], name_mangle = mangle_name(src["name"])
        dup["email"] = src["email"].upper() if (src["email"] and random.random() < 0.6) else ""
        email_mangle = "email_upper" if dup["email"] else ("email_blank" if src["email"] else "email_missing_in_both")
        dup["phone"] = fake.phone_number() if random.random() < 0.3 else src["phone"]   # conflicting value
        phone_mangle = "phone_same" if dup["phone"] == src["phone"] else "phone_changed"
        truth_dups.append({"duplicate_id": dup_id, "original_id": src["lead_id"],
                           "mangle_type": "|".join([company_mangle, name_mangle, email_mangle, phone_mangle])})
        dup["source"] = random.choice(SOURCES)
        dup["last_contact_date"] = (date.fromisoformat(src["last_contact_date"]) - timedelta(days=random.randint(5, 90))).isoformat()
        leads.append(dup)
        # the duplicate record also carries some of the activity -> merging matters
        for _ in range(random.randint(1, 4)):
            a += 1
            activity.append({
                "activity_id": f"A{a:06d}",
                "lead_id": dup_id,
                "type": random.choices(types, weights=weights)[0],
                "activity_date": (TODAY - timedelta(days=random.randint(0, 60))).isoformat(),
            })

    random.shuffle(leads)

    # ---- 6. harder messiness ---------------------------------------------------------
    # Uses its own random generator and only runs after everything above, so steps 1-5 (and the
    # committed sample data they produce) stay exactly the same.
    rng = random.Random(seed + 1)
    fake2 = Faker(["en_US", "en_IN", "en_GB"])
    fake2.seed_instance(seed + 1)
    base = sorted((l for l in leads if int(l["lead_id"][1:]) <= n_leads), key=lambda l: l["lead_id"])
    used = {t["original_id"] for t in truth_dups}
    extra = []
    for src in rng.sample([l for l in base if l["lead_id"] not in used and l["email"]], int(n_leads * 0.03)):
        next_id += 1
        dup = dict(src)
        dup["lead_id"] = f"L{next_id:05d}"
        dup["name"], name_mangle = mangle_name(src["name"], rng)
        kind = rng.choices(["renamed", "typo", "conflict"], weights=[4, 4, 2])[0]
        if kind == "renamed":        # company rebranded / acquired -> new name, new email domain
            dup["company"] = f"{fake2.last_name()} {rng.choice(SUFFIXES)}"
            company_mangle = "company_renamed"
            if rng.random() < 0.5:
                dup["email"] = src["email"].split("@")[0] + "@" + dup["company"].split()[0].lower() + ".com"
                email_mangle = "email_new_domain"
            else:
                dup["email"], email_mangle = "", "email_blank"
            keep_phone = rng.random() < 0.8
        else:
            dup["company"], company_mangle = mangle_company(src["company"], rng)
            if kind == "typo":
                dup["email"], email_mangle = typo_email(src["email"], rng)
                keep_phone = rng.random() < 0.5
            else:                    # conflicting title + phone, email re-typed in capitals or left blank
                dup["email"], email_mangle = (src["email"].upper(), "email_upper") if rng.random() < 0.5 else ("", "email_blank")
                keep_phone = False
        if kind == "conflict" or rng.random() < 0.3:
            dup["title"] = rng.choice([t for t in TITLES if t != src["title"]])
        dup["phone"] = src["phone"] if keep_phone else fake2.phone_number()
        dup["source"] = rng.choice(SOURCES)
        dup["last_contact_date"] = max(date.fromisoformat(src["created_at"]),
                                       date.fromisoformat(src["last_contact_date"]) - timedelta(days=rng.randint(5, 90))).isoformat()
        extra.append(dup)
        truth_dups.append({"duplicate_id": dup["lead_id"], "original_id": src["lead_id"],
                           "mangle_type": "|".join([company_mangle, name_mangle, email_mangle,
                                                    "phone_same" if keep_phone else "phone_changed"])})
        for _ in range(rng.randint(1, 4)):
            a += 1
            activity.append({"activity_id": f"A{a:06d}", "lead_id": dup["lead_id"],
                             "type": rng.choices(types, weights=weights)[0],
                             "activity_date": (TODAY - timedelta(days=rng.randint(0, 60))).isoformat()})
    # field-level problems on existing records: missing phone numbers, badly formatted emails
    for lead in rng.sample(base, int(n_leads * 0.03)):
        lead["phone"] = ""
    for lead in rng.sample([l for l in base if l["email"]], int(n_leads * 0.01)):
        lead["email"], _ = typo_email(lead["email"], rng, invalid=True)
    for dup in extra:                # scatter the new duplicates without reordering the other rows
        leads.insert(rng.randint(0, len(leads)), dup)
    n_dups += len(extra)

    pd.DataFrame(leads).to_csv(OUT / "leads.csv", index=False)
    pd.DataFrame(deals).to_csv(OUT / "deals.csv", index=False)
    pd.DataFrame(activity).to_csv(OUT / "activity.csv", index=False)
    pd.DataFrame(notes).to_csv(OUT / "notes.csv", index=False)
    pd.DataFrame(truth_dups).to_csv(OUT / "truth_duplicates.csv", index=False)
    print(f"leads={len(leads)} (incl. {n_dups} hidden duplicates)  deals={len(deals)}  "
          f"activity={len(activity)}  notes={len(notes)}  -> {OUT}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--leads", type=int, default=2000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--today", default=DEFAULT_TODAY, help="reference date for the generated data (YYYY-MM-DD)")
    args = p.parse_args()
    TODAY = date.fromisoformat(args.today)
    main(args.leads, args.seed)
