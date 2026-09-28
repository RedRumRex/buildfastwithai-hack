"""
Ranking evaluation for the decision engine: precision@20 against planted hot leads.

    python data/plant_hot_leads.py      # once, plants hot leads + decoys and writes the truth file
    python eval/eval_ranking.py         # prints the report and writes eval/results_ranking.md

precision@20 = (# truly hot leads among the top 20 ranked leads) / 20      target >= 80%

"Truly hot" = the planted hot leads + any organic lead that passes the SAME sales-manager checklist
used to design them (reachable, contacted in 30d, open deal >= $20k in Demo/Proposal/Negotiation
closing within 60d, a demo/meeting/pricing visit in 30d, no recent objection). The checklist is a
hard AND of facts – it never looks at the scoring weights – so it is an independent yardstick.
We ALSO report the strict version (planted leads only), which is a lower bound because the random
sample data contains some genuinely hot leads that nobody planted.

We also report, for every weight preset:
  - precision@20 in the default queue view (stale leads hidden, as the app shows it)
  - decoys that reach the top 20 (should be 0)
  - median rank of the planted hot leads, and how many made the top 50
  - what the non-planted top-20 leads are: organic leads that pass the same sales-manager
    checklist are arguably hot too, so they are shown separately instead of hidden.
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRUTH = ROOT / "data" / "truth_hot_leads.csv"
K = 20
TARGET = 0.80


def load_truth(path: Path = TRUTH, leads: pd.DataFrame | None = None) -> pd.DataFrame | None:
    """Load the planted ground truth. If `leads` is given, return None when the truth file doesn't
    match the data (e.g. the sample data was regenerated without re-running plant_hot_leads.py)."""
    if not Path(path).exists():
        return None
    truth = pd.read_csv(path)
    if leads is not None:
        comp = leads.set_index("lead_id")["company"]
        ok = truth["lead_id"].isin(comp.index).all() and (
            truth["lead_id"].map(comp).astype(str).str.strip() == truth["company"].astype(str).str.strip()).all()
        if not ok:
            return None
    return truth


def precision_at_k(scores: pd.DataFrame, hot_ids, k: int = K) -> float:
    """hot_ids: a set of lead ids, or the truth DataFrame (then its planted 'hot' rows are used)."""
    if isinstance(hot_ids, pd.DataFrame):
        hot_ids = set(hot_ids.loc[hot_ids["label"] == "hot", "lead_id"])
    top = scores.sort_values(["score", "lead_id"], ascending=[False, True], kind="mergesort").head(k)["lead_id"]
    return float(top.isin(set(hot_ids)).sum()) / k


def truly_hot_ids(scores: pd.DataFrame, truth: pd.DataFrame) -> set:
    """Planted hot leads + organic (non-planted) leads that pass the checklist. Independent of weights."""
    planted = set(truth["lead_id"])
    organic = {r["lead_id"] for r in scores.to_dict("records") if r["lead_id"] not in planted and meets_hot_checklist(r)}
    return set(truth.loc[truth["label"] == "hot", "lead_id"]) | organic


def meets_hot_checklist(row: dict) -> bool:
    """Same checklist used to design the planted hot leads (all must hold) – applied to organic leads."""
    comps = {c["component"]: c for c in row["components"]}
    eng = comps.get("Engagement (last 30 days)", {}).get("fact", "")
    cid = row.get("close_in_days")
    return (not row["is_stale"] and row["days_since_contact"] <= 30
            and row["top_deal_amount"] >= 20_000
            and row["top_deal_stage"] in ("Demo Scheduled", "Proposal Sent", "Negotiation")
            and cid is not None and not pd.isna(cid) and cid <= 60
            and any(x in eng for x in ("demo request", "meeting", "pricing page"))
            and row.get("note_signal", 0) >= 0)


def evaluate(scores: pd.DataFrame, truth: pd.DataFrame, k: int = K) -> dict:
    s = scores.sort_values(["score", "lead_id"], ascending=[False, True], kind="mergesort").reset_index(drop=True)
    s["rank"] = s.index + 1
    hot = set(truth.loc[truth["label"] == "hot", "lead_id"])
    decoy = set(truth.loc[truth["label"] == "decoy", "lead_id"])
    top = s.head(k)
    other = top[~top["lead_id"].isin(hot | decoy)]
    visible = s[~s["is_stale"]]
    hot_ranks = s.loc[s["lead_id"].isin(hot), "rank"]
    true_hot = truly_hot_ids(s, truth)
    return {
        f"precision@{k}": precision_at_k(s, true_hot, k),
        f"precision@{k} (strict, planted only)": precision_at_k(s, hot, k),
        f"precision@{k} (stale hidden)": precision_at_k(visible, true_hot, k),
        "n organic truly hot": len(true_hot - hot),
        f"decoys in top {k}": int(top["lead_id"].isin(decoy).sum()),
        "median rank of hot": float(hot_ranks.median()),
        "hot in top 50": int((hot_ranks <= 50).sum()),
        "n hot planted": len(hot),
        f"organic in top {k} that pass hot checklist": int(sum(meets_hot_checklist(r) for r in other.to_dict("records"))),
        f"organic in top {k} failing checklist": int(len(other) - sum(meets_hot_checklist(r) for r in other.to_dict("records"))),
        "_top": top, "_decoy_ranks": s.loc[s["lead_id"].isin(decoy)].merge(truth, on="lead_id")[["rank", "lead_id", "profile", "score"]],
    }


def main():
    from core.clean import clean
    from core.scoring import PRESETS, apply_weights, extract_signals

    dfs = [pd.read_csv(ROOT / "data" / f"{t}.csv") for t in ["leads", "deals", "activity", "notes"]]
    truth = load_truth(leads=dfs[0])
    if truth is None:
        sys.exit("No valid data/truth_hot_leads.csv for this data – run `python data/plant_hot_leads.py` first.")
    res = clean(*dfs)
    missing = set(truth["lead_id"]) - set(res.leads["lead_id"])
    if missing:
        print(f"WARNING: {len(missing)} planted leads were merged away by dedupe: {sorted(missing)[:5]}")
    sig = extract_signals(res.leads, res.deals, res.activity, res.notes, res.ref_date)

    lines = ["# Ranking evaluation (precision@20)", "",
             f"Dataset date {res.ref_date.date()} · {len(res.leads):,} clean leads · "
             f"{(truth.label == 'hot').sum()} planted hot leads · {(truth.label == 'decoy').sum()} planted decoys · target ≥ {TARGET:.0%}", "",
             "| Weights | **precision@20** | strict (planted only) | stale hidden | decoys in top 20 | planted hot in top 50 | median planted-hot rank |",
             "|---|---|---|---|---|---|---|"]
    results = {}
    for name, w in PRESETS.items():
        r = evaluate(apply_weights(sig, w), truth)
        results[name] = r
        lines.append(f"| {name} | **{r['precision@20']:.0%}** | {r['precision@20 (strict, planted only)']:.0%} | "
                     f"{r['precision@20 (stale hidden)']:.0%} | {r['decoys in top 20']} | "
                     f"{r['hot in top 50']}/{r['n hot planted']} | {r['median rank of hot']:.0f} |")

    d = results["Balanced (default)"]
    lines += ["", f"Organic (not planted) leads that pass the hot checklist: {d['n organic truly hot']}. "
              "They count as truly hot in the main column and as misses in the strict column.",
              "", "## Default weights – top 20", ""]
    hot = set(truth.loc[truth.label == "hot", "lead_id"])
    t = d["_top"].copy()
    t["truth"] = t["lead_id"].map(truth.set_index("lead_id")["profile"]).fillna(
        t.apply(lambda r: "organic (passes checklist)" if meets_hot_checklist(r.to_dict()) else "organic", axis=1))
    lines += ["| rank | lead | company | score | truth |", "|---|---|---|---|---|"]
    lines += [f"| {r['rank']} | {r['lead_id']} | {r['company']} | {r['score']} | {r['truth']} |" for r in t.to_dict("records")]
    lines += ["", "## Where the decoys rank (default weights)", "", "| rank | lead | decoy type | score |", "|---|---|---|---|"]
    lines += [f"| {r['rank']} | {r['lead_id']} | {r['profile']} | {r['score']} |"
              for r in d["_decoy_ranks"].sort_values("rank").to_dict("records")]
    report = "\n".join(lines) + "\n"
    (ROOT / "eval" / "results_ranking.md").write_text(report)
    print(report)
    ok = d["precision@20"] >= TARGET and d["decoys in top 20"] == 0
    print(f"{'PASS' if ok else 'FAIL'}: default precision@20 = {d['precision@20']:.0%} (target {TARGET:.0%}), "
          f"decoys in top 20 = {d['decoys in top 20']}")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
