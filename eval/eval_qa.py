"""
QA evaluation: text-to-SQL execution accuracy, citation validity, fallback rate.

    python eval/eval_qa.py                 # full run, writes eval/results_qa.md
    python eval/eval_qa.py --sleep 3       # slower, for free-tier rate limits (Groq ~30 req/min)
    python eval/eval_qa.py --limit 10 --no-write

Execution accuracy: the predicted SQL is run and compared with the hand-written gold SQL
(eval/qa_testset.json). Match = same number of rows AND every gold column's values appear
(as a multiset) in some predicted column. Extra predicted columns are fine; column order / names
don't matter; numbers are compared rounded to 2 decimals.

Citation validity is measured on the RAW model output (before the checker removes bad ids), over
8 notes questions + the SQL answer summaries + 10 lead explanations. The checker then guarantees
what the user sees is 100% valid.
"""
import argparse
import json
import math
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TESTSET = ROOT / "eval" / "qa_testset.json"
TARGET_ACC, TARGET_CIT = 0.85, 1.0


def _norm(v):
    if isinstance(v, bool):
        return str(v)
    try:
        if pd.isna(v):
            return "NULL"
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)) or type(v).__name__ == "Decimal":
        f = float(v)
        return "NULL" if math.isnan(f) else f"{round(f, 2):.2f}"
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return str(v)[:10]
    return str(v).strip()


def _cols(df):
    return [sorted(_norm(x) for x in df[c].tolist()) for c in df.columns]


def results_match(gold: pd.DataFrame, pred: pd.DataFrame) -> bool:
    if len(gold) != len(pred):
        return False
    pcols, used = _cols(pred), set()
    for g in _cols(gold):
        hit = next((i for i, p in enumerate(pcols) if i not in used and p == g), None)
        if hit is None:
            return False
        used.add(hit)
    return True


def build_store():
    from core.clean import clean
    from core.qa import DataStore
    from core.scoring import apply_weights, extract_signals
    dfs = [pd.read_csv(ROOT / "data" / f"{t}.csv") for t in ["leads", "deals", "activity", "notes"]]
    res = clean(*dfs)
    sig = extract_signals(res.leads, res.deals, res.activity, res.notes, res.ref_date)
    scores = apply_weights(sig)
    return DataStore(res, scores), scores


def main(limit: int | None, sleep: float, write: bool, n_explain: int) -> bool:
    from core import actions, llm
    from core.qa import answer
    t0 = time.time()
    store, scores = build_store()
    ref = str(store.ref_date.date())
    ts = json.loads(TESTSET.read_text())
    qs = ts["sql_questions"][:limit] if limit else ts["sql_questions"]
    mode = f"LLM `{llm.model_name()}`" if llm.configured() else "NO LLM KEY (rules only)"

    rows, cites = [], {"cited": 0, "invalid": 0, "bad": []}

    def add_cites(rep, where):
        cites["cited"] += len(rep["cited"])
        cites["invalid"] += len(rep["invalid"])
        cites["bad"] += [f"{where}: {i}" for i in rep["invalid"]]

    for q in qs:
        gold = store.run_sql(q["gold_sql"].replace("{ref}", ref), limit=10 ** 7)
        try:
            out = answer(store, q["question"], summarize=True)
            ok, err = False, ""
            if out["type"] != "sql":
                err = "routed to notes"
            else:
                pred = store.run_sql(out["sql"], limit=10 ** 7)
                ok = results_match(gold, pred)
                if not ok:
                    err = f"rows gold={len(gold)} pred={len(pred)}"
            add_cites(out.get("citations", {"cited": [], "invalid": []}), f"Q{q['id']}")
            fb = bool(out.get("fallback"))
            sql = out.get("sql") or ""
        except Exception as e:
            ok, fb, sql, err = False, True, "", f"{type(e).__name__}: {str(e)[:120]}"
        rows.append({"id": q["id"], "question": q["question"], "ok": ok, "fallback": fb, "sql": sql, "error": err})
        print(f"Q{q['id']:>2} {'OK ' if ok else 'BAD'} {'(fallback)' if fb else '':10} {q['question']}  {err}")
        if llm.configured():
            time.sleep(sleep)

    notes_rows = []
    for nq in ts["notes_questions"]:
        out = answer(store, nq)
        rep = out.get("citations", {"cited": [], "invalid": []})
        add_cites(rep, f"notes '{nq}'")
        notes_rows.append({"q": nq, "engine": out["engine"], "fallback": out.get("fallback"),
                           "cited": len(rep["cited"]), "invalid": len(rep["invalid"]), "hits": len(out["table"])})
        if llm.configured():
            time.sleep(sleep)

    for r in scores.sort_values("score", ascending=False).head(n_explain).to_dict("records"):
        _, rep = actions.explain_checked(r)
        add_cites(rep, f"explain {r['lead_id']}")
        if llm.configured():
            time.sleep(sleep)

    n = len(rows)
    acc = sum(r["ok"] for r in rows) / n
    fb_rate = sum(r["fallback"] for r in rows) / n
    llm_rows = [r for r in rows if not r["fallback"]]
    acc_llm = sum(r["ok"] for r in llm_rows) / len(llm_rows) if llm_rows else float("nan")
    cit = 1 - cites["invalid"] / cites["cited"] if cites["cited"] else 1.0

    L = ["# QA evaluation (text-to-SQL + RAG citations)", "",
         f"Mode: {mode} · RAG engine: **{store.rag_engine}** · dataset date {ref} · {n} SQL questions · "
         f"{len(notes_rows)} notes questions · {n_explain} explanations · runtime {time.time() - t0:.0f}s", "",
         "| metric | value | target |", "|---|---|---|",
         f"| execution accuracy (all {n}) | **{acc:.0%}** | ≥ {TARGET_ACC:.0%} |",
         f"| execution accuracy (LLM-answered only, {len(llm_rows)}) | {acc_llm:.0%} | |",
         f"| fallback rate (rules used) | {fb_rate:.0%} | low |",
         f"| citation validity, raw model output ({cites['cited']} ids) | **{cit:.1%}** | 100% |",
         f"| citation validity shown to user (after checker) | 100% | 100% |", ""]
    if cites["bad"]:
        L += ["Invalid ids produced by the model (removed by the checker): " + ", ".join(cites["bad"][:30]), ""]
    L += ["## SQL questions", "", "| # | question | correct | fallback | note |", "|---|---|---|---|---|"]
    L += [f"| {r['id']} | {r['question']} | {'✅' if r['ok'] else '❌'} | {'yes' if r['fallback'] else ''} | {r['error']} |" for r in rows]
    L += ["", "## Notes questions (RAG)", "", "| question | engine | hits | ids cited | invalid |", "|---|---|---|---|---|"]
    L += [f"| {r['q']} | {r['engine']} | {r['hits']} | {r['cited']} | {r['invalid']} |" for r in notes_rows]
    L += ["", "## Failed SQL", ""]
    L += [f"- Q{r['id']} {r['question']}\n  ```sql\n  {r['sql']}\n  ```" for r in rows if not r["ok"] and r["sql"]] or ["None."]
    report = "\n".join(L)
    if write:
        (ROOT / "eval" / "results_qa.md").write_text(report, encoding="utf-8")
    print("\n" + "\n".join(L[:12]))
    ok = acc >= TARGET_ACC and cites["invalid"] == 0
    print(("PASS" if ok else "FAIL") + f" accuracy {acc:.0%} (target {TARGET_ACC:.0%}), raw citation validity {cit:.1%}, fallback {fb_rate:.0%}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--sleep", type=float, default=2.0, help="seconds between LLM questions (rate limits)")
    ap.add_argument("--explain", type=int, default=10, help="how many lead explanations to citation-check")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args()
    sys.exit(0 if main(a.limit, a.sleep, not a.no_write, a.explain) else 1)
