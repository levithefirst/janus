"""The eval harness runs end to end through the real job pipeline, and its mock-AI numbers are pinned.

The pinned counts are what `python -m eval.run_eval --ai mock` produced when eval/results-mock.md was
written. If a detector change moves them, this test fails on purpose: rerun the eval, look at what
changed, and commit the new results file together with the change.
"""
from eval.run_eval import report, run


def test_eval_runs_and_matches_the_committed_mock_results():
    res = run("mock")
    assert res["n"] == 13 and res["good_cases"] == 4 and res["bad_cases"] == 9
    assert sum(c["false_accept"] for c in res["by_category"].values()) == res["false_accept"]
    assert (res["false_accept"], res["false_reject"], res["spurious_reuse"]) == (1, 1, 2)
    fa = [r["id"] for r in res["rows"] if r["false_accept"]]
    fr = [r["id"] for r in res["rows"] if r["false_reject"]]
    assert fa == ["edited-pasted"] and fr == ["good-outlet"]
    md = report(res)
    assert "not a measurement of real-world error rates" in md and "False accepts: 1 of 9" in md
