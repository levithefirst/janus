"""Backend product gaps: capture per checklist item, scope amendments, disputes, provider re-upload."""
from decimal import Decimal

import pytest

from conftest import PHOTOS, authorized, make_config, new_threshold, with_exif
from janus import rules
from janus.ai import MockAI
from janus.models import RequirementCheck, Verdict
from janus.rules import RuleViolation
from janus.service import Janus, JanusError
from janus.state import State, TransitionError


class ItemAI(MockAI):
    """Per-item verdicts from a queue: [{R1: ("met", .95), ...}, ...]."""
    def __init__(self, rounds):
        self.rounds = list(rounds)

    def verify(self, compiled, *a, **k):
        r = self.rounds.pop(0)
        checks = [RequirementCheck(requirement_id=x.id, result=r.get(x.id, ("cannot_tell", 0.2))[0],
                                   confidence=r.get(x.id, ("cannot_tell", 0.2))[1], observation=f"reasoning for {x.id}")
                  for x in compiled.requirements]
        overall = "met" if all(c.result == "met" for c in checks) else "uncertain"
        return Verdict(verdict=overall, confidence=0.6, checks=checks, summary="items", next_step=None)


ALLOC = {"R1": "6.00", "R2": "6.00", "R3": "6.00", "R4": "6.00"}
MET, NOT, LOW = ("met", 0.95), ("not_met", 0.95), ("met", 0.50)


def itemized(tmp_path, rounds, consent_from=("buyer",)):
    j = Janus(make_config(tmp_path), ai=ItemAI(rounds))
    t = new_threshold(j)
    j.set_allocations(t.id, ALLOC)
    j.consent(t.id, evidence_from=list(consent_from))
    j.create_payment(t.id)
    return j, j.authorize(t.id)


def photo(i):
    return with_exif(["received_match.jpg", "received_mismatch.jpg", "listing_red_mug.jpg"][0], scale=1 - i * 0.05)


def mock_auth(j, t):
    return j.paypal.auth(j.get(t.id).payment.authorization_id)


# ---------- capture per checklist item ----------

def test_allocations_validated(janus):
    t = new_threshold(janus)
    with pytest.raises(JanusError, match="every checklist item"):
        janus.set_allocations(t.id, {"R1": "24.00"})
    with pytest.raises(JanusError, match="add up"):
        janus.set_allocations(t.id, dict(ALLOC, R4="1.00"))
    janus.consent(t.id)
    janus.create_payment(t.id)
    with pytest.raises(TransitionError):
        janus.set_allocations(t.id, ALLOC)


def test_partial_then_final_capture(tmp_path):
    j, t = itemized(tmp_path, [{"R1": MET, "R2": MET, "R4": LOW}, {"R3": MET, "R4": NOT}])
    t = j.submit_and_verify(t.id, photo(0))
    assert t.state == State.HELD and t.item_status == {"R1": "captured", "R2": "captured", "R3": "pending", "R4": "pending"}
    assert [c.amount for c in t.payment.captures] == ["12.00"] and not t.payment.captures[0].final
    assert mock_auth(j, t)["status"] == "PARTIALLY_CAPTURED"
    t = j.submit_and_verify(t.id, photo(1))
    assert t.state == State.CAPTURED and t.item_status["R4"] == "released"
    assert [c.amount for c in t.payment.captures] == ["12.00", "6.00"] and t.payment.captures[-1].final
    assert mock_auth(j, t)["captured"] == "18.00" and mock_auth(j, t)["status"] == "CAPTURED"
    assert [c.request_id for c in t.payment.captures] == [f"janus-{t.id}-capture-1", f"janus-{t.id}-capture-2"]


def test_all_items_fail_voids(tmp_path):
    j, t = itemized(tmp_path, [{r: NOT for r in ALLOC}])
    t = j.submit_and_verify(t.id, photo(0))
    assert t.state == State.VOIDED and not t.payment.captures


def test_partial_then_rest_fails_voids_remainder(tmp_path):
    j, t = itemized(tmp_path, [{"R1": MET}, {"R2": NOT, "R3": NOT, "R4": NOT}])
    j.submit_and_verify(t.id, photo(0))
    t = j.submit_and_verify(t.id, photo(1))
    assert t.state == State.VOIDED and [c.amount for c in t.payment.captures] == ["6.00"]
    assert mock_auth(j, t)["captured"] == "6.00"


def test_buyer_release_captures_only_pending_items(tmp_path):
    j, t = itemized(tmp_path, [{"R1": MET, "R2": NOT}])
    j.submit_and_verify(t.id, photo(0))
    t = j.release(t.id)
    assert t.state == State.CAPTURED and t.payment.captures[-1].amount == "12.00"  # R3 + R4, not R2
    assert sum(Decimal(c.amount) for c in t.payment.captures) == Decimal("18.00")


def test_item_rule_blocks_low_confidence_or_double_capture(tmp_path):
    j, t = itemized(tmp_path, [{"R1": MET, "R2": LOW}])
    t = j.submit_and_verify(t.id, photo(0))
    x = j.get(t.id)
    from janus.store import Pending
    for items in (["R2"], ["R1"]):  # R2 low confidence, R1 already captured
        x.pending = Pending(action="capture", by="janus", reason="r", request_id="x", amount="6.00",
                            items=items, verdict_index=0, at="now")
        x.verdicts[0].action = "capture"
        with pytest.raises(RuleViolation):
            rules.check_capture(x, "janus", 0.85)


# ---------- amendments ----------

def test_pending_amendment_blocks_auto_capture(janus):
    t = authorized(janus)
    janus.propose_amendment(t.id, price="20.00", reason="one fewer coat of paint")
    t = janus.submit_and_verify(t.id, with_exif("received_match.jpg"))
    assert t.state == State.HELD and not t.payment.captures
    assert any(e["kind"] == "rule_blocked" and e["data"]["rule"] == "amendment_pending"
               for e in janus.audit.entries(t.id))


def test_accepted_amendment_captures_new_amount(janus):
    t = authorized(janus)
    janus.propose_amendment(t.id, price="20.00")
    old_hash = rules.terms_hash(janus.get(t.id))
    t = janus.accept_amendment(t.id)
    assert t.consents[-1].kind == "amendment" and t.consents[-1].terms_hash != old_hash
    t = janus.submit_and_verify(t.id, with_exif("received_match.jpg"))
    assert t.state == State.CAPTURED and t.payment.captures[0].amount == "20.00"


def test_rejected_amendment_keeps_original_terms(janus):
    t = authorized(janus)
    janus.propose_amendment(t.id, condition="Pay if it is any mug")
    janus.reject_amendment(t.id)
    t = janus.submit_and_verify(t.id, with_exif("received_match.jpg"))
    assert t.state == State.CAPTURED and t.payment.captures[0].amount == "24.00"


def test_amendment_cannot_exceed_authorized_amount(janus):
    t = authorized(janus)
    with pytest.raises(JanusError, match="new authorization"):
        janus.propose_amendment(t.id, price="30.00")


# ---------- provider re-upload, silent customer, dispute ----------

def test_provider_evidence_never_auto_captures_without_customer(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, with_exif("received_match.jpg"), by="seller")
    assert t.state == State.HELD and "silent customer" in t.last_verdict.reason and not t.payment.captures


def test_provider_evidence_captures_when_customer_agreed_to_it(tmp_path):
    j = Janus(make_config(tmp_path))
    t = new_threshold(j)
    j.consent(t.id, evidence_from=["buyer", "seller"])
    j.create_payment(t.id)
    j.authorize(t.id)
    assert j.submit_and_verify(t.id, with_exif("received_match.jpg"), by="seller").state == State.CAPTURED


def test_provider_reupload_after_hold(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg", by="seller")
    t = janus.submit_evidence(t.id, with_exif("received_match.jpg"), by="seller")
    assert t.state == State.EVIDENCE_SUBMITTED and [e.by for e in t.evidence] == ["seller", "seller"]


def test_dispute_freezes_then_resolves(janus):
    t = authorized(janus)
    t = janus.dispute(t.id, "wrong colour")
    with pytest.raises(TransitionError):
        janus.submit_evidence(t.id, with_exif("received_match.jpg"), by="buyer")
    t = janus.submit_evidence(t.id, with_exif("received_match.jpg"), by="seller")  # provider answers
    assert t.state == State.DISPUTED
    with pytest.raises(TransitionError):
        janus.verify(t.id)
    with pytest.raises(JanusError):
        janus.dispute(t.id, " ")
    assert janus.withdraw_dispute(t.id).state == State.HELD


def test_dispute_can_end_in_refund_or_release(janus):
    a = authorized(janus)
    janus.dispute(a.id, "x")
    assert janus.refund(a.id).state == State.VOIDED
    b = authorized(janus)
    janus.dispute(b.id, "x")
    assert janus.release(b.id).state == State.CAPTURED
