"""Hard rules: each one is attacked directly, including with a policy that has been broken on purpose."""
import pytest

from conftest import PHOTOS, authorized, make_config, new_threshold
from janus import rules
from janus.ai import MockAI
from janus.models import RequirementCheck, Verdict
from janus.policy import Decision
from janus.rules import RuleViolation
from janus.service import Janus, JanusError
from janus.state import State, TransitionError
from janus.store import ImmutableError


class FixedAI(MockAI):
    """Returns whatever verdict the test wants, so we can probe the rules around the AI."""
    def __init__(self, verdict="met", confidence=0.95, result="met"):
        self.v = (verdict, confidence, result)

    def verify(self, compiled, listing, evidence, root, ref=None):
        verdict, conf, result = self.v
        return Verdict(verdict=verdict, confidence=conf, summary="fixed", next_step=None,
                       checks=[RequirementCheck(requirement_id=r.id, result=result, observation="o")
                               for r in compiled.requirements])


def jan(tmp_path, **ai):
    return Janus(make_config(tmp_path), ai=FixedAI(**ai))


def auth_status(j, t):
    return j.paypal.get_authorization(t.id, j.get(t.id).payment.authorization_id)["status"]


# ---------- rule 1: consent ----------

def test_no_payment_without_consent(janus):
    t = new_threshold(janus)
    with pytest.raises(JanusError, match="consent"):
        janus.create_payment(t.id)


def test_capture_blocked_when_consent_is_for_other_terms(janus):
    t = authorized(janus)
    with janus.store.lock(t.id):  # terms change behind the customer's back
        x = janus.get(t.id)
        x.condition = "Pay if the seller says so"
        janus.store.save(x)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.HELD and "consent" in t.events[-1]["reason"]
    assert auth_status(janus, t) == "CREATED"  # no money moved
    assert any(e["kind"] == "rule_blocked" for e in janus.audit.entries(t.id))


def test_check_capture_without_any_consent(janus):
    t = authorized(janus)
    x = janus.get(t.id)
    x.consents.clear()
    with pytest.raises(RuleViolation, match="consent"):
        rules.check_capture(x, "janus", 0.85)
    with pytest.raises(RuleViolation, match="consent"):
        rules.check_capture(x, "buyer", 0.85)


def test_buyer_release_records_its_own_consent(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    t = janus.release(t.id)
    assert t.state == State.CAPTURED and t.consents[-1].kind == "release"


# ---------- rule 2: confidence ----------

@pytest.mark.parametrize("conf", [0.0, 0.5, 0.84, 0.849])
def test_below_threshold_always_holds(tmp_path, conf):
    j = jan(tmp_path, confidence=conf)
    t = authorized(j)
    t = j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.HELD and auth_status(j, t) == "CREATED"


@pytest.mark.parametrize("verdict,result", [("not_met", "not_met"), ("met", "met")])
def test_low_confidence_holds_even_if_policy_is_broken(tmp_path, monkeypatch, verdict, result):
    """Break the policy so it always says capture. The rule still refuses a low-confidence capture."""
    j = jan(tmp_path, verdict=verdict, confidence=0.40, result=result)
    import janus.service as svc
    monkeypatch.setattr(svc, "decide", lambda *a, **k: Decision("capture", "broken policy"))
    t = authorized(j)
    t = j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.HELD and auth_status(j, t) == "CREATED"
    assert [e for e in j.audit.entries(t.id) if e["kind"] == "rule_blocked"][0]["data"]["rule"] == "confidence"


def test_threshold_edge_is_inclusive(tmp_path):
    j = jan(tmp_path, confidence=0.85)
    t = authorized(j)
    assert j.submit_and_verify(t.id, PHOTOS / "received_match.jpg").state == State.CAPTURED


# ---------- rule 3: never capture after void or expiry ----------

def test_no_capture_after_void(janus):
    t = authorized(janus)
    janus.refund(t.id)
    with pytest.raises(TransitionError):
        janus.release(t.id)
    with pytest.raises(RuleViolation, match="no_capture_after_void"):
        x = janus.get(t.id)
        x.state = State.HELD  # even if the state were somehow wrong, the rule looks at the payment
        rules.check_capture(x, "buyer", 0.85)


def test_no_capture_after_expiry(janus):
    t = authorized(janus)
    janus.paypal.expire(t.payment.authorization_id)
    janus.submit_evidence(t.id, PHOTOS / "received_unclear.jpg")
    assert janus.refresh(t.id).state == State.EXPIRED
    with pytest.raises(TransitionError):
        janus.release(t.id)
    assert not janus.get(t.id).payment.captures


def test_no_capture_past_expiration_time_even_if_paypal_status_lags(tmp_path, monkeypatch):
    j = jan(tmp_path)
    t = authorized(j)
    with j.store.lock(t.id):
        x = j.get(t.id)
        x.payment.expiration_time = "2000-01-01T00:00:00+00:00"
        j.store.save(x)
    monkeypatch.setattr(j.paypal, "get_authorization", lambda *a, **k: {"status": "CREATED"})
    j.submit_evidence(t.id, PHOTOS / "received_match.jpg")
    t = j.verify(t.id)
    assert t.state != State.CAPTURED and not t.payment.captures


# ---------- rule 4: terminal is final ----------

def test_terminal_threshold_rejects_every_operation(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_mismatch.jpg")
    assert t.state == State.VOIDED
    ops = [lambda: janus.consent(t.id), lambda: janus.release(t.id), lambda: janus.refund(t.id),
           lambda: janus.submit_evidence(t.id, PHOTOS / "received_match.jpg"), lambda: janus.verify(t.id),
           lambda: janus.retry_settlement(t.id), lambda: janus.create_payment(t.id)]
    for op in ops:
        with pytest.raises((TransitionError, JanusError, RuleViolation, ImmutableError)):
            op()
    assert janus.reconcile(t.id).state == State.VOIDED
    assert janus.get(t.id).version == t.version


# ---------- rule 5: silent customer ----------

@pytest.mark.parametrize("who", ["scheduler", "webhook", "paypal", "system", "seller", "anyone"])
def test_only_ai_path_or_customer_can_capture(janus, who):
    t = authorized(janus)
    with pytest.raises(RuleViolation, match="silent_customer"):
        rules.check_capture(janus.get(t.id), who, 0.85)


# ---------- crash recovery ----------

def test_crash_after_paypal_captured_before_save_recovers_once(tmp_path, monkeypatch):
    j = Janus(make_config(tmp_path))
    t = authorized(j)
    real = j.paypal.capture_authorization

    def capture_then_die(*a, **k):
        real(*a, **k)               # PayPal has taken the money...
        raise SystemExit("crash")   # ...and the process dies before saving
    monkeypatch.setattr(j.paypal, "capture_authorization", capture_then_die)
    with pytest.raises(SystemExit):
        j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert j.get(t.id).state == State.CAPTURING

    j2 = Janus(make_config(tmp_path))  # restart
    [t2] = j2.recover()
    assert t2.state == State.CAPTURED and len(t2.payment.captures) == 1
    captures = [c for c in j2.paypal._db()["captures"].values()]
    assert len(captures) == 1 and captures[0]["id"] == t2.payment.captures[0].capture_id


def test_crash_before_paypal_call_recovers_once(tmp_path, monkeypatch):
    j = Janus(make_config(tmp_path))
    t = authorized(j)
    monkeypatch.setattr(j.paypal, "capture_authorization", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    j2 = Janus(make_config(tmp_path))
    assert [x.state for x in j2.recover()] == [State.CAPTURED]
    assert len(j2.paypal._db()["captures"]) == 1
    assert j2.recover() == []  # nothing left to recover; running it again does nothing


def test_audit_records_every_decision(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    kinds = [e["kind"] for e in janus.audit.entries(t.id)]
    for k in ("condition_compiled", "consent", "authorized", "verdict", "capture_requested", "captured"):
        assert k in kinds, k
    assert all(e["reason"] for e in janus.audit.entries(t.id))
    assert janus.audit.verify()[0]
