"""
Dedupe evaluation for the cleaning layer: precision / recall of merged duplicate pairs.

    python data/generate_data.py        # writes data/truth_duplicates.csv next to the CSVs
    python eval/eval_dedupe.py          # prints the report and writes eval/results_dedupe.md
    python eval/eval_dedupe.py --data some/dir --no-write    # evaluate another generated dataset

Ground truth = every duplicate the generator injected (duplicate_id -> original_id, plus how it was
mangled). Everything is scored on PAIRS of raw lead records:
  true pair   = two raw records that really are the same person (from the truth file)
  merged pair = two raw records that clean() folded into the same surviving lead
  recall      = true pairs merged / all true pairs                  target >= 98%
  precision   = merged pairs that are true / all merged pairs
  false merge = a merged pair that is NOT a true pair (two different people fused)   target 0

Planted hot leads / decoys (data/truth_hot_leads.csv) are not in the truth file, so if dedupe ever
folds one into another record it shows up as a false merge, and it is also reported separately.
"""
import argparse
import sys
from itertools import combinations
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TABLES = ["leads", "deals", "activity", "notes"]
TARGET_RECALL = 0.98


def cluster_of(res) -> dict:
    """raw lead_id -> surviving lead_id, read from the cleaned leads' merged_from column."""
    out = {}
    for lid, merged in zip(res.leads["lead_id"], res.leads["merged_from"]):
        out[lid] = lid
        for x in filter(None, str(merged).split(", ")):
            out[x] = lid
    return out


def pairs(groups) -> set:
    return {frozenset(p) for g in groups for p in combinations(sorted(g), 2)}


def evaluate(res, truth: pd.DataFrame, raw_leads: pd.DataFrame) -> dict:
    master = cluster_of(res)
    true_groups = {}
    for dup, orig in zip(truth["duplicate_id"], truth["original_id"]):
        true_groups.setdefault(orig, {orig}).add(dup)
    true_pairs = pairs(true_groups.values())
    merged_groups = {}
    for lid, m in master.items():
        merged_groups.setdefault(m, set()).add(lid)
    merged_pairs = pairs(g for g in merged_groups.values() if len(g) > 1)

    tp = true_pairs & merged_pairs
    missed = true_pairs - merged_pairs
    false = merged_pairs - true_pairs

    t = truth.copy()
    t["found"] = [frozenset((d, o)) in merged_pairs for d, o in zip(t["duplicate_id"], t["original_id"])]
    parts = t["mangle_type"].str.split("|", expand=True)
    by_type = pd.concat([
        t.assign(kind=parts[i]).groupby("kind")["found"].agg(found="sum", total="count")
        for i in range(parts.shape[1])
    ]).reset_index()

    raw = raw_leads.set_index("lead_id")
    fmt = lambda i: f"{i}: {str(raw.at[i, 'name']).strip()} @ {raw.at[i, 'company']} <{'' if pd.isna(raw.at[i, 'email']) else raw.at[i, 'email']}>"
    return {
        "raw_records": len(raw_leads),
        "clean_records": len(res.leads),
        "true_pairs": len(true_pairs),
        "merged_pairs": len(merged_pairs),
        "recall": len(tp) / len(true_pairs) if true_pairs else 1.0,
        "precision": len(tp) / len(merged_pairs) if merged_pairs else 1.0,
        "false_merges": len(false),
        "by_type": by_type,
        "by_rule": res.merges["rule"].value_counts(),
        "missed": [(fmt(a), fmt(b), t.loc[t["duplicate_id"].isin([a, b]), "mangle_type"].iloc[0])
                   for a, b in (sorted(p) for p in missed)],
        "false": [(fmt(a), fmt(b)) for a, b in (sorted(p) for p in false)],
    }


def report(r: dict, planted_lost: list, data_dir: Path) -> str:
    ok = r["recall"] >= TARGET_RECALL and r["false_merges"] == 0
    lines = [
        "# Dedupe evaluation (entity resolution)", "",
        f"Data: `{data_dir.relative_to(ROOT) if data_dir.is_relative_to(ROOT) else data_dir}` · "
        f"{r['raw_records']:,} raw leads → {r['clean_records']:,} clean leads · "
        f"{r['true_pairs']} injected duplicate pairs · target recall ≥ {TARGET_RECALL:.0%}, 0 false merges", "",
        "| **recall** | **precision** | **false merges** | true pairs merged | pairs merged in total | planted leads merged away |",
        "|---|---|---|---|---|---|",
        f"| **{r['recall']:.1%}** | **{r['precision']:.1%}** | **{r['false_merges']}** | "
        f"{round(r['recall'] * r['true_pairs'])}/{r['true_pairs']} | {r['merged_pairs']} | {len(planted_lost)} |", "",
        f"**{'PASS' if ok else 'FAIL'}**", "",
        "## Recall by how the duplicate was mangled", "",
        "Each duplicate has a company, a name and an email mangle, so it is counted once in each group.", "",
        "| mangle | found | total | recall |", "|---|---|---|---|",
    ]
    lines += [f"| {x['kind']} | {x['found']} | {x['total']} | {x['found'] / x['total']:.0%} |"
              for x in r["by_type"].to_dict("records")]
    lines += ["", "## Merges by rule", "", "| rule | merges |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in r["by_rule"].items()]
    lines += ["", "## Missed duplicates", ""]
    lines += ([f"- {a}  ~  {b}  ({m})" for a, b, m in r["missed"]] or ["None."])
    lines += ["", "## False merges", ""]
    lines += ([f"- {a}  ≠  {b}" for a, b in r["false"]] or ["None."])
    if planted_lost:
        lines += ["", "## Planted hot leads / decoys merged away", "", ", ".join(planted_lost)]
    return "\n".join(lines) + "\n"


def main(data_dir: Path, write: bool) -> bool:
    from core.clean import clean

    truth_path = data_dir / "truth_duplicates.csv"
    if not truth_path.exists():
        sys.exit(f"No {truth_path} – run `python data/generate_data.py` first.")
    truth = pd.read_csv(truth_path)
    dfs = [pd.read_csv(data_dir / f"{t}.csv") for t in TABLES]
    missing = set(truth["duplicate_id"]) | set(truth["original_id"])
    missing -= set(dfs[0]["lead_id"])
    if missing:
        sys.exit(f"{len(missing)} lead ids in the truth file are not in leads.csv – was the data regenerated "
                 "without its truth file?")
    res = clean(*dfs)
    r = evaluate(res, truth, dfs[0])

    planted_lost = []
    hot_path = data_dir / "truth_hot_leads.csv"
    if hot_path.exists():
        planted = set(pd.read_csv(hot_path)["lead_id"]) & set(dfs[0]["lead_id"])
        planted_lost = sorted(planted - set(res.leads["lead_id"]))

    text = report(r, planted_lost, data_dir)
    if write:
        (ROOT / "eval" / "results_dedupe.md").write_text(text)
    print(text)
    ok = r["recall"] >= TARGET_RECALL and r["false_merges"] == 0
    print(f"{'PASS' if ok else 'FAIL'}: recall = {r['recall']:.1%} (target {TARGET_RECALL:.0%}), "
          f"precision = {r['precision']:.1%}, false merges = {r['false_merges']}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=ROOT / "data", help="folder with the CSVs + truth_duplicates.csv")
    ap.add_argument("--no-write", action="store_true", help="don't overwrite eval/results_dedupe.md")
    a = ap.parse_args()
    sys.exit(0 if main(a.data.resolve(), not a.no_write) else 1)
