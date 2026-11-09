from conftest import ROOT
from janus.ai import MockAI
from janus.calibration import report, run, summarize


def test_harness_runs_and_pipeline_is_safe_on_the_set(tmp_path):
    from janus.logs import setup
    setup(tmp_path)
    res = run(MockAI(), ROOT / "calibration" / "labels.json", ROOT, tmp_path / "work")
    assert res["n"] == 12 and res["unsafe_after_gate"] == 0
    gated = {r["id"] for r in res["rows"] if r["gate_blocked"]}
    assert {"blurred", "dark-blurry"} <= gated and "match" not in gated
    md = report(res, "mock")
    assert "small, synthetic set" in md and "does not certify accuracy" in md


def test_metrics():
    rows = [{"confidence": 0.9, "correct": True, "unsafe": False, "needless_hold": False, "gate_blocked": False,
             "label": "met"},
            {"confidence": 0.9, "correct": False, "unsafe": True, "needless_hold": False, "gate_blocked": False,
             "label": "not_met"}]
    s = summarize(rows, 0.85)
    assert s["accuracy"] == 0.5 and s["unsafe_after_gate"] == 1
    assert abs(s["brier"] - (0.01 + 0.81) / 2) < 1e-9 and abs(s["ece"] - 0.4) < 1e-9
