"""The settlement rule. Plain code, not AI: the AI's verdict is an input, this decides the money.

CAPTURE only when the AI says the condition is met, is confident, and every critical
requirement was checked and met. VOID only when the AI says it is not met, is confident,
and at least one critical requirement clearly failed. Everything else is HOLD.
A verdict that contradicts its own per-requirement checks is also HOLD.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import CompiledCondition, Verdict

Action = Literal["capture", "void", "hold"]
DEFAULT_MIN_CONFIDENCE = 0.85


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


def decide(verdict: Verdict, condition: CompiledCondition, min_confidence: float = DEFAULT_MIN_CONFIDENCE) -> Decision:
    conf = max(0.0, min(1.0, float(verdict.confidence)))
    critical = {r.id for r in condition.requirements if r.critical} or {r.id for r in condition.requirements}
    results = {c.requirement_id: c.result for c in verdict.checks}
    unchecked = sorted(critical - results.keys())
    failed = sorted(r for r in critical if results.get(r) == "not_met")
    unclear = sorted(r for r in critical if results.get(r) == "cannot_tell")

    if verdict.verdict == "uncertain":
        return Decision("hold", f"AI could not decide (confidence {conf:.2f})")
    if conf < min_confidence:
        return Decision("hold", f"confidence {conf:.2f} is below the {min_confidence:.2f} needed to move money")
    if verdict.verdict == "met":
        if failed or unclear or unchecked:
            bad = ", ".join(failed + unclear + unchecked)
            return Decision("hold", f"verdict 'met' contradicts critical requirement(s) {bad}")
        return Decision("capture", f"all {len(critical)} critical requirement(s) met (confidence {conf:.2f})")
    # verdict == not_met
    if not failed:
        return Decision("hold", "verdict 'not_met' but no critical requirement was shown to fail")
    return Decision("void", f"critical requirement(s) {', '.join(failed)} not met (confidence {conf:.2f})")
