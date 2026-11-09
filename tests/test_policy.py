"""The settlement rule: money moves only on a confident, internally consistent verdict."""
import pytest

from janus.models import CompiledCondition, Requirement, RequirementCheck, Verdict
from janus.policy import decide

COND = CompiledCondition(summary="s", evidence_instructions="i", not_verifiable=[], requirements=[
    Requirement(id="R1", statement="red", check="c", critical=True),
    Requirement(id="R2", statement="star", check="c", critical=True),
    Requirement(id="R3", statement="glossy", check="c", critical=False)])


def v(verdict, conf, r1="met", r2="met", r3="met"):
    return Verdict(verdict=verdict, confidence=conf, summary="s", next_step=None, checks=[
        RequirementCheck(requirement_id=i, result=r, observation="o") for i, r in (("R1", r1), ("R2", r2), ("R3", r3))])


@pytest.mark.parametrize("verdict,expected", [
    (v("met", 0.95), "capture"),
    (v("met", 0.95, r3="cannot_tell"), "capture"),          # non-critical doesn't block
    (v("not_met", 0.95, r2="not_met"), "void"),
    (v("met", 0.80), "hold"),                               # below threshold: never settle
    (v("not_met", 0.60, r1="not_met"), "hold"),
    (v("uncertain", 0.99, r1="cannot_tell"), "hold"),
    (v("met", 0.99, r2="cannot_tell"), "hold"),             # contradicts its own checks
    (v("met", 0.99, r2="not_met"), "hold"),
    (v("not_met", 0.99), "hold"),                           # 'not met' with nothing failing
    (v("met", 1.7), "capture"),                             # out-of-range confidence clamped
])
def test_decide(verdict, expected):
    assert decide(verdict, COND).action == expected


def test_missing_critical_check_holds():
    verdict = Verdict(verdict="met", confidence=0.99, summary="s", next_step=None,
                      checks=[RequirementCheck(requirement_id="R1", result="met", observation="o")])
    d = decide(verdict, COND)
    assert d.action == "hold" and "R2" in d.reason


def test_threshold_is_configurable():
    assert decide(v("met", 0.9), COND, min_confidence=0.95).action == "hold"
