"""
Demo check – runs the whole 3-minute demo headlessly and fails if ANY step errors.
Target metric from plan.md: "the full demo runs with zero errors".

    python eval/demo_check.py          # ~20-60 s, exit code 0 = safe to demo / merge

It clicks through the same steps as the README demo script, in a real Streamlit session:
Data health -> Action queue (Why? panel, plain-English filter, approve, edit + save, download,
reject, bulk approve, undo) -> Ask your data (all example questions) -> Audit log (filters,
export) -> How scoring works (sliders, preset, reset) + the ranking evaluation.
Decisions go to a throw-away audit file, so the real data/audit_log.csv is never touched.
"""
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["LEADLENS_AUDIT_FILE"] = str(Path(tempfile.mkdtemp()) / "demo_check_audit.csv")

from streamlit.testing.v1 import AppTest  # noqa: E402

steps = []


def step(name, fn):
    t = time.time()
    try:
        detail = fn() or ""
        err = [str(e.value) for e in at.exception]
        ok = not err
        detail = detail if ok else err[0][:200]
    except Exception as e:  # a failed assertion or a missing widget – prefer the app's own error if it has one
        app_err = [str(x.value) for x in at.exception]
        ok, detail = False, (f"app error: {app_err[0]}" if app_err else f"{type(e).__name__}: {e}")[:200]
    steps.append((ok, name, time.time() - t, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name:<48} {time.time() - t:5.1f}s  {detail}")
    return ok


def has_download(*keys):
    """Download buttons with these keys exist. Older Streamlit test tools (1.50) don't expose widget keys
    for download buttons – then fall back to counting them."""
    els = at.get("download_button")
    found = {getattr(e, "key", None) for e in els}
    if found - {None}:
        return set(keys) <= found
    return len(els) >= len(keys)


def queue():
    return [x for x in at.expander if x.label[:1] in "#✅❌"]


def btn(prefix):
    return [b for b in at.button if b.key and b.key.startswith(prefix)]


at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)


def s_load():
    at.run()
    return f"{len(queue())} leads in queue"


def s_health():
    m = {x.label: x.value for x in at.metric}
    assert "Duplicates merged" in m and "Clean leads" in m, "health metrics missing"
    return f"{m['Duplicates merged']} duplicates merged, {m['Clean leads']} clean leads"


def s_why():
    at.toggle[0].set_value(True).run()
    return "source rows shown"


def s_filter():
    at.text_input(key="nl").input("skip anyone contacted in the last 2 weeks, FinTech only, over $20k").run()
    chips = [m.value for m in at.markdown if "Understood by" in m.value]
    assert chips, "filter not understood"
    n = len(queue())
    at.text_input(key="nl").input("").run()
    return f"{n} leads after filter"


def s_approve():
    b = btn("ap_")[0]
    lid = b.key[3:]
    b.click().run()
    assert at.session_state["decisions"][lid]["decision"] == "approved"
    return f"approved {lid}, email drafted"


def s_edit():
    lid = next(k for k, d in at.session_state["decisions"].items() if d["decision"] == "approved")
    at.text_area(key=f"body_{lid}").input(at.text_area(key=f"body_{lid}").value + "\n\nP.S. demo check").run()
    at.button(key=f"save_{lid}").click().run()
    assert at.session_state["decisions"][lid]["body"].endswith("demo check")
    assert has_download(f"dl_{lid}"), "no .eml download"
    return "edit saved + .eml download ready"


def s_reject():
    b = btn("rj_")[0]
    b.click().run()
    return f"rejected {b.key[3:]}"


def s_bulk():
    [b for b in at.button if b.label == "Select all"][0].click().run()
    n = len(at.multiselect(key="bulk_sel").value)
    assert n, "nothing to bulk-approve"
    at.button(key="bulk_approve").click().run()
    return f"bulk-approved {n}"


def s_undo():
    b = btn("undo_")[0]
    b.click().run()
    return f"undid {b.key[5:]}"


def s_ask():
    for i in range(6):
        at.button(key=f"ex_{i}").click().run()
        if at.exception:
            break
    return f"{len(at.session_state['chat'])} answers"


def s_audit():
    at.multiselect(key="aud_dec").select("approved").run()
    cap = [c.value for c in at.caption if c.value.startswith("Showing")]
    at.multiselect(key="aud_dec").unselect("approved").run()
    assert has_download("aud_dl_f", "aud_dl_all"), "export buttons missing"
    return cap[0] if cap else ""


def s_weights():
    at.slider(key="w_deal_size").set_value(0.0).run()
    a = queue()[0].label
    at.selectbox(key="preset").set_value("Close this quarter").run()
    [b for b in at.button if b.label == "Apply preset"][0].click().run()
    [b for b in at.button if b.label.startswith("↺")][0].click().run()
    assert at.slider(key="w_deal_size").value == 30.0, "reset failed"
    return "re-ranked live, preset + reset ok"


def s_ranking():
    m = {x.label: x.value for x in at.metric}
    p = m.get("Truly hot leads in top 20 (precision@20)")
    assert p is not None, "ranking metric missing (run data/plant_hot_leads.py?)"
    assert int(p.rstrip("%")) >= 80, f"precision@20 {p} below 80% target"
    return f"precision@20 = {p}"


t0 = time.time()
print(f"LeadLens demo check – {ROOT}\n")
if step("App loads (clean, dedupe, score)", s_load):
    for name, fn in [("Data health metrics", s_health), ("Why? panel shows source rows", s_why),
                     ("Plain-English filter", s_filter), ("Approve a lead (email drafted)", s_approve),
                     ("Edit + save + download the email", s_edit), ("Reject a lead", s_reject),
                     ("Bulk approve", s_bulk), ("Undo a decision", s_undo),
                     ("Ask your data: all example questions", s_ask), ("Audit log filters + CSV export", s_audit),
                     ("Scoring weights: slider, preset, reset", s_weights), ("Ranking target (precision@20 >= 80%)", s_ranking)]:
        step(name, fn)

n_fail = sum(not ok for ok, *_ in steps)
print(f"\n{len(steps) - n_fail}/{len(steps)} steps passed in {time.time() - t0:.0f}s – "
      + ("DEMO READY ✅" if not n_fail else "NOT READY ❌ – fix the failing steps before demoing or merging"))
sys.exit(1 if n_fail else 0)
