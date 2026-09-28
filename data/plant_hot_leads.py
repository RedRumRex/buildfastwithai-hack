"""
Plant KNOWN hot leads (and look-alike decoys) into the sample CRM so we can measure
how well the decision engine ranks them  ->  precision@20 in eval/eval_ranking.py.

Run AFTER data/generate_data.py (it post-processes the CSVs, so it keeps working whatever
Member 1 changes in the generator):

    python data/generate_data.py
    python data/plant_hot_leads.py            # idempotent: re-running replaces the old plants

Writes data/truth_hot_leads.csv  (lead_id, company, label, profile, why)

Ground truth is defined by a SALES-MANAGER CHECKLIST, not by our scoring weights:
  HOT   = several independent buying signals at the same time – reachable, contacted recently,
          a real open deal moving to close soon, high-intent activity (demo / meeting / pricing),
          and no recent objection. Strength varies on purpose (not every hot lead is maxed out).
  DECOY = looks great on ONE signal but a rep should NOT call it this week
          (huge deal but unreachable, lost to a competitor, only opens emails, slipping deal).
Planted leads use normal-looking ids and fresh company names, so nothing but the data gives them away.
"""
import argparse
import random
import re
from datetime import timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

DATA = Path(__file__).parent
TRUTH = DATA / "truth_hot_leads.csv"
TABLES = ["leads", "deals", "activity", "notes"]

INDUSTRIES = ["FinTech", "Healthcare", "E-commerce", "Manufacturing", "EdTech",
              "Logistics", "Retail", "Media", "Real Estate", "Telecom"]
TARGET = ["FinTech", "Healthcare", "E-commerce", "Logistics"]
SIZES = [25, 50, 120, 250, 500, 1200, 3000]
TITLES = ["Head of Sales", "VP Operations", "CTO", "Founder", "IT Director", "COO", "CFO"]
SUFFIXES = ["Inc.", "Corp", "Ltd", "LLC", "Pvt Ltd", "Technologies", "Solutions", "Group"]
POSITIVE = [
    "Budget approved for this quarter, wants a proposal by end of month.",
    "Champion is pushing internally, needs a security questionnaire filled.",
    "Current contract with competitor ends soon, actively evaluating us.",
    "Asked for enterprise pricing and SSO details, strong buying signal.",
    "Decision maker joined the call, wants to start a pilot next month.",
]
NEUTRAL = ["Asked for a follow-up call with their technical team.",
           "Evaluating three vendors, we are on the shortlist."]


def _num(ids: pd.Series) -> int:
    return int(ids.astype(str).str.extract(r"(\d+)")[0].astype(int).max())


def _norm_company(s: str) -> str:
    s = re.sub(r"\b(inc|corp|corporation|ltd|llc|pvt|private|limited|technologies|solutions|group|co)\b\.?", " ", s.lower())
    return re.sub(r"[^a-z0-9]", "", s)


class Planter:
    def __init__(self, dfs: dict, seed: int):
        self.rng = random.Random(seed)
        self.fake = Faker(["en_US", "en_IN", "en_GB"])
        self.fake.seed_instance(seed)
        L, D, A, N = (dfs[t] for t in TABLES)
        # plants stay ON or BEFORE the latest date already in the data, so the dataset's "today" doesn't move
        self.T = max(pd.to_datetime(A["activity_date"]).max(), pd.to_datetime(L["last_contact_date"]).max()).date()
        self.next = {"L": _num(L["lead_id"]), "D": _num(D["deal_id"]), "A": _num(A["activity_id"]), "N": _num(N["note_id"])}
        self.used_companies = set(L["company"].map(_norm_company))
        self.used_last = set(L["name"].astype(str).str.split().str[-1].str.lower())
        self.rows = {t: [] for t in TABLES}
        self.truth = []

    def _id(self, p, width):
        self.next[p] += 1
        return f"{p}{self.next[p]:0{width}d}"

    def _day(self, lo, hi):   # a date lo..hi days before "today"
        return (self.T - timedelta(days=self.rng.randint(lo, hi))).isoformat()

    def lead(self, label, profile, why, *, industry=None, size=None, last_contact=(2, 14), email_ok=True,
             bounced=False, created=(60, 500)):
        while True:   # fresh company + surname so the lead can't be merged into an existing record
            last = self.fake.last_name()
            company = f"{self.fake.last_name()} {self.rng.choice(SUFFIXES)}"
            if _norm_company(company) not in self.used_companies and last.lower() not in self.used_last:
                break
        self.used_companies.add(_norm_company(company))
        self.used_last.add(last.lower())
        first = self.fake.first_name()
        lid = self._id("L", 5)
        lo, hi = last_contact
        self.rows["leads"].append({
            "lead_id": lid, "name": f"{first} {last}", "company": company,
            "email": f"{first.lower()}.{last.lower()}@{company.split()[0].lower()}.com" if email_ok else "",
            "phone": self.fake.phone_number(), "title": self.rng.choice(TITLES),
            "industry": industry or self.rng.choice(INDUSTRIES), "company_size": size or self.rng.choice(SIZES),
            "country": self.rng.choice(["India", "USA", "UK", "Germany", "Singapore"]),
            "source": self.rng.choice(["Website", "Webinar", "Referral", "LinkedIn Ads"]),
            "created_at": self._day(max(created[0], hi + 1), max(created[1], hi + 2)),
            "last_contact_date": self._day(lo, hi), "email_status": "bounced" if bounced else "valid",
        })
        self.truth.append({"lead_id": lid, "company": company, "label": label, "profile": profile, "why": why})
        return lid, company

    def deal(self, lid, company, amount, stage, close_in, kind="Platform"):
        self.rows["deals"].append({
            "deal_id": self._id("D", 5), "lead_id": lid, "deal_name": f"{company.split()[0]} - {kind}",
            "amount_usd": int(amount) // 100 * 100, "stage": stage,
            "expected_close_date": (self.T + timedelta(days=close_in)).isoformat(),
            "last_stage_change": self._day(3, 25)})

    def acts(self, lid, types, window=(0, 21)):
        for t in types:
            self.rows["activity"].append({"activity_id": self._id("A", 6), "lead_id": lid, "type": t,
                                          "activity_date": self._day(*window)})

    def note(self, lid, text, window=(1, 30)):
        self.rows["notes"].append({"note_id": self._id("N", 5), "lead_id": lid, "note_date": self._day(*window),
                                   "author": self.rng.choice(["Aarav (AE)", "Meera (SDR)", "Rohan (AE)", "Sara (SDR)"]),
                                   "text": text})

    # ----------------------------------------------------------------- HOT
    def hot(self, n):
        r = self.rng
        for i in range(n):
            strength = ["strong", "strong", "medium", "medium", "moderate"][i % 5]
            lid, co = self.lead("hot", f"hot_{strength}", "multi-signal: reachable + open deal closing soon + high-intent activity + no objection",
                                industry=r.choice(TARGET) if r.random() < 0.5 else None)
            amount = {"strong": r.uniform(60e3, 180e3), "medium": r.uniform(30e3, 90e3), "moderate": r.uniform(20e3, 45e3)}[strength]
            stage = r.choice(["Proposal Sent", "Negotiation"] if strength != "moderate" else ["Demo Scheduled", "Proposal Sent"])
            close = {"strong": r.randint(5, 30), "medium": r.randint(10, 40), "moderate": r.randint(20, 55)}[strength]
            self.deal(lid, co, amount, stage, close, r.choice(["Platform", "Enterprise", "Expansion", "Pilot"]))
            intent = r.sample(["demo_request", "meeting", "pricing_page_visit", "call"], k={"strong": 3, "medium": 2, "moderate": 2}[strength])
            filler = r.choices(["email_open", "email_click"], k=r.randint(1, 4))
            self.acts(lid, intent + filler)
            if r.random() < {"strong": 0.9, "medium": 0.6, "moderate": 0.4}[strength]:
                self.note(lid, r.choice(POSITIVE))
            else:
                self.note(lid, r.choice(NEUTRAL))

    # ----------------------------------------------------------------- DECOYS
    def decoys(self, n_each):
        r = self.rng
        for _ in range(n_each):
            # big deal, but the contact is unreachable (stale + bounced email)
            lid, co = self.lead("decoy", "big_but_unreachable", "biggest deal size, but no contact in 200+ days and email bounced",
                                last_contact=(200, 320), bounced=True, size=3000)
            self.deal(lid, co, r.uniform(150e3, 250e3), "Proposal Sent", r.randint(5, 40), "Enterprise")
            self.acts(lid, ["email_open"], window=(40, 120))

            # lots of activity + big deal, but they told us they chose a competitor
            lid, co = self.lead("decoy", "lost_to_competitor", "strong deal + activity, but latest call note says they went with a competitor",
                                industry=r.choice(TARGET))
            self.deal(lid, co, r.uniform(70e3, 140e3), "Negotiation", r.randint(5, 30))
            self.acts(lid, ["meeting", "pricing_page_visit", "call", "email_open", "email_click"])
            self.note(lid, "Went with a competitor, might revisit next year.", window=(1, 6))

            # opens every email, but no deal and no real intent
            lid, co = self.lead("decoy", "email_opener", "many email opens only; no deal, no meeting, no demo",
                                industry=r.choice(TARGET), size=1200)
            self.acts(lid, ["email_open"] * r.randint(10, 16) + ["email_click"])

            # large deal that has quietly slipped: close date passed, no recent activity
            lid, co = self.lead("decoy", "slipping_deal", "large deal but close date passed weeks ago and no recent activity",
                                last_contact=(45, 58))
            self.deal(lid, co, r.uniform(90e3, 160e3), "Qualified", -r.randint(20, 60))
            self.acts(lid, ["email_open", "call"], window=(40, 90))


def main(n_hot: int = 30, n_decoy_each: int = 4, seed: int = 7):
    dfs = {t: pd.read_csv(DATA / f"{t}.csv", dtype=str) for t in TABLES}
    if TRUTH.exists():   # remove the previous plants first (idempotent) …
        prev = pd.read_csv(TRUTH, dtype=str)
        comp = dfs["leads"].set_index("lead_id")["company"]
        # … but only rows that really are our plants (same id AND same company). After the data is
        # regenerated, an old truth id may now belong to a genuine lead – that one must not be touched.
        old = {l for l, c in zip(prev["lead_id"], prev.get("company", pd.Series(dtype=str)))
               if l in comp.index and str(comp[l]).strip() == str(c).strip()}
        dfs = {t: df[~df["lead_id"].isin(old)] for t, df in dfs.items()}
    p = Planter(dfs, seed)
    p.hot(n_hot)
    p.decoys(n_decoy_each)
    for t in TABLES:
        new = pd.DataFrame(p.rows[t], columns=dfs[t].columns).astype(str)
        if t == "leads":   # scatter plants among existing rows (not at the bottom) without reordering the rest
            rs = random.Random(seed)
            new["_pos"] = [rs.uniform(0, len(dfs[t])) for _ in range(len(new))]
            old_ = dfs[t].assign(_pos=range(len(dfs[t])))
            out = pd.concat([old_, new]).sort_values("_pos", kind="mergesort").drop(columns="_pos")
        else:
            out = pd.concat([dfs[t], new], ignore_index=True)
        out.to_csv(DATA / f"{t}.csv", index=False)
    pd.DataFrame(p.truth).to_csv(TRUTH, index=False)
    print(f"planted {n_hot} hot leads + {4 * n_decoy_each} decoys (reference date {p.T}) -> {TRUTH.name}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hot", type=int, default=30)
    ap.add_argument("--decoys", type=int, default=4, help="decoys per decoy type (4 types)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    main(a.hot, a.decoys, a.seed)
