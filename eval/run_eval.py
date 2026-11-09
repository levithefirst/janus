"""Run the eval set through the real job pipeline and print false-accept and false-reject counts.

    python -m eval.run_eval              # AI from the environment (mock without ANTHROPIC_API_KEY)
    python -m eval.run_eval --ai mock
    python -m eval.run_eval --ai claude  # needs ANTHROPIC_API_KEY
    python -m eval.run_eval --write eval/results-mock.md

PayPal is always the offline mock here: the eval measures the packet, not payments.

false accept = a case labelled 'flag' whose packet came out clean (capturable with no override)
false reject = a case labelled 'clean' whose packet was flagged, held for review, or recommended rejection
spurious reuse flag = a reused_photo flag on a photo that was not reused (no case reuses a before photo, and
                      only the reused-* cases reuse an after photo). It can sit on a case whose final label is
                      right for another reason, so it is counted separately rather than hidden.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(ai_mode: str) -> dict:
    from eval.cases import DESCRIPTIONS, build
    from janus.config import Config
    from janus.service import Janus
    from janus.state import State

    labels = json.loads((ROOT / "eval" / "labels.json").read_text())
    tmp = Path(tempfile.mkdtemp(prefix="janus-eval-"))
    base = Config.from_env()
    cfg = Config(paypal_mode="mock", paypal_client_id="", paypal_client_secret="", ai_mode=ai_mode,
                 model=base.model, data_dir=tmp, base_url="http://localhost:8000", janus_secret="e" * 40)
    j = Janus(cfg)
    outputs: dict[str, bytes] = {}
    rows = []
    for case in labels["cases"]:
        kind, before, after = build(case["id"], outputs)
        outputs[case["id"]] = after
        title, desc = DESCRIPTIONS[kind]
        t = j.create_job(title, "100.00", desc)
        j.consent(t.id, channel="eval")
        j.create_payment(t.id)
        j.authorize(t.id)
        j.submit_evidence(t.id, before, by="seller", phase="before")
        j.submit_evidence(t.id, after, by="seller", phase="after")
        t = j.verify(t.id)
        rec = t.last_verdict
        reused_after = case["id"] in ("reused-cross-job", "reused-cropped")
        spurious = sum(1 for e in t.evidence if any(f.startswith("reused_photo") for f in e.flags)
                       and (e.phase == "before" or not reused_after))
        clean = t.state == State.PACKET_READY and rec.action == "recommend_accept" and not rec.blocking_flags
        got = "clean" if clean else "flag"
        rows.append({"id": case["id"], "category": case["category"], "expected": case["expected"], "got": got,
                     "state": t.state.value, "recommendation": rec.action, "confidence": round(rec.verdict.confidence, 2),
                     "scene": rec.verdict.scene_match, "edit": rec.verdict.edit_signs,
                     "flags": sorted({f.split(":")[0] for f in rec.blocking_flags}), "spurious_reuse": spurious,
                     "false_accept": case["expected"] == "flag" and got == "clean",
                     "false_reject": case["expected"] == "clean" and got == "flag"})
    by_cat = {}
    for cat in dict.fromkeys(r["category"] for r in rows):
        rs = [r for r in rows if r["category"] == cat]
        by_cat[cat] = {"n": len(rs), "false_accept": sum(r["false_accept"] for r in rs),
                       "false_reject": sum(r["false_reject"] for r in rs)}
    return {"ai": j.ai.mode, "model": base.model if j.ai.mode == "claude" else None, "n": len(rows),
            "false_accept": sum(r["false_accept"] for r in rows), "false_reject": sum(r["false_reject"] for r in rows),
            "bad_cases": sum(r["expected"] == "flag" for r in rows), "good_cases": sum(r["expected"] == "clean" for r in rows),
            "spurious_reuse": sum(r["spurious_reuse"] for r in rows), "by_category": by_cat, "rows": rows, "flag_counts": dict(Counter(f for r in rows for f in r["flags"]))}


def report(res: dict) -> str:
    who = f"{res['ai']}" + (f" ({res['model']})" if res["model"] else "")
    out = [f"# Eval results: AI {who}", "",
           f"{res['n']} synthetic cases (drawings, not photographs). Small: this shows which defences fire on these",
           "cases; it is not a measurement of real-world error rates.", "",
           f"- **False accepts: {res['false_accept']} of {res['bad_cases']}** bad cases came out clean (capturable with no override)",
           f"- **False rejects: {res['false_reject']} of {res['good_cases']}** good cases were flagged or held",
           f"- Spurious reuse flags: {res['spurious_reuse']} photos flagged as reused that were not", "",
           "| Category | n | False accept | False reject |", "|---|---|---|---|"]
    out += [f"| {c} | {v['n']} | {v['false_accept']} | {v['false_reject']} |" for c, v in res["by_category"].items()]
    out += ["", "| Case | Expected | Got | State | Recommendation | Confidence | Scene | Edit | Blocking flags | Spurious reuse |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["rows"]:
        mark = " (FALSE ACCEPT)" if r["false_accept"] else (" (FALSE REJECT)" if r["false_reject"] else "")
        out.append(f"| {r['id']} | {r['expected']} | {r['got']}{mark} | {r['state']} | {r['recommendation']} | "
                   f"{r['confidence']:.2f} | {r['scene']} | {r['edit']} | {', '.join(r['flags']) or '-'} | "
                   f"{r['spurious_reuse'] or '-'} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    import sys
    sys.path.insert(0, str(ROOT))
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ai", choices=["mock", "claude"], default=None)
    p.add_argument("--write", help="also write the Markdown report to this file")
    a = p.parse_args(argv)
    from janus.config import Config
    res = run(a.ai or Config.from_env().ai_mode)
    md = report(res)
    print(md)
    print(f"FALSE ACCEPTS: {res['false_accept']}   FALSE REJECTS: {res['false_reject']}   "
          f"SPURIOUS REUSE FLAGS: {res['spurious_reuse']}")
    if a.write:
        Path(a.write).write_text(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
