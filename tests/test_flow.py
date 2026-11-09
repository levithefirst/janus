"""The full loop against mock PayPal + mock AI: all three outcomes and the failure paths."""
import pytest

from conftest import PHOTOS, authorized, new_threshold
from janus.paypal import PayPalError
from janus.service import JanusError
from janus.state import State, TransitionError


def mock_auth(j, t):
    return j.paypal.get_authorization(t.id, t.payment.authorization_id)["status"]


def test_create_compiles_condition(janus):
    t = new_threshold(janus)
    assert t.state == State.CREATED and t.amount == "24.00"
    assert t.compiled.requirements and all(r.id.startswith("R") for r in t.compiled.requirements)
    assert t.listing.photos == ["listing/L1.jpg"]
    assert janus.get(t.id).compiled == t.compiled


@pytest.mark.parametrize("bad", [dict(price="0"), dict(price="abc"), dict(condition=" "), dict(title="")])
def test_create_validates(janus, bad):
    args = dict(title="Mug", price="10", condition="Pay if it matches") | bad
    with pytest.raises(JanusError):
        janus.create(**args)


def test_match_captures(janus):
    t = authorized(janus)
    assert mock_auth(janus, t) == "CREATED"
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg", "here it is")
    assert t.state == State.CAPTURED
    assert t.last_verdict.verdict.verdict == "met" and t.last_verdict.action == "capture"
    assert t.outcome.by == "janus" and t.payment.capture_id
    assert mock_auth(janus, t) == "CAPTURED"


def test_mismatch_voids(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_mismatch.jpg")
    assert t.state == State.VOIDED and t.payment.voided and not t.payment.capture_id
    assert t.last_verdict.action == "void"
    assert mock_auth(janus, t) == "VOIDED"
    with pytest.raises(TransitionError):
        janus.release(t.id)  # can't capture a voided authorization


def test_unclear_holds_then_better_photo_captures(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    assert t.state == State.HELD and mock_auth(janus, t) == "CREATED"  # no money moved
    assert t.last_verdict.verdict.next_step
    with pytest.raises(TransitionError):
        janus.verify(t.id)  # needs new evidence first
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.CAPTURED and len(t.verdicts) == 2


def test_held_buyer_can_release(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    t = janus.release(t.id)
    assert t.state == State.CAPTURED and t.outcome.by == "buyer"


def test_held_seller_can_refund(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    t = janus.refund(t.id)
    assert t.state == State.VOIDED and t.outcome.by == "seller"


def test_verdict_and_paypal_calls_logged(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    log = (janus.config.data_dir / "janus.log.jsonl").read_text()
    for needle in ('"verdict"', '"capture"', '"condition_compiled"', '"get_authorization"'):
        assert needle in log


@pytest.mark.parametrize("step", ["authorize", "evidence", "verify", "release", "refund"])
def test_out_of_order_rejected(janus, step):
    t = new_threshold(janus)
    calls = {"authorize": lambda: janus.authorize(t.id),
             "evidence": lambda: janus.submit_evidence(t.id, note="x"),
             "verify": lambda: janus.verify(t.id),
             "release": lambda: janus.release(t.id),
             "refund": lambda: janus.refund(t.id)}
    with pytest.raises(TransitionError):
        calls[step]()
    assert janus.get(t.id).state == State.CREATED


# ---------- idempotency ----------

def test_payment_and_authorize_are_idempotent(janus):
    t = new_threshold(janus)
    o1 = janus.create_payment(t.id).payment.order_id
    assert janus.create_payment(t.id).payment.order_id == o1
    a1 = janus.authorize(t.id).payment.authorization_id
    assert janus.authorize(t.id).payment.authorization_id == a1  # return URL hit twice


def test_authorize_adopts_existing_authorization(janus):
    """Crash after PayPal authorized but before we saved: the next call adopts it, no double hold."""
    t = new_threshold(janus)
    janus.create_payment(t.id)
    t = janus.get(t.id)
    auth = janus.paypal.authorize_order(t.id, t.payment.order_id, f"janus-{t.id}-authorize")  # never recorded
    t = janus.authorize(t.id)
    assert t.state == State.AUTHORIZED and t.payment.authorization_id == auth["authorization_id"]
    assert any(e["action"] == "authorization_adopted" for e in t.events)


def test_not_approved_yet(janus, monkeypatch):
    t = new_threshold(janus)
    janus.create_payment(t.id)
    monkeypatch.setattr(janus.paypal, "get_order", lambda *a: {"status": "PAYER_ACTION_REQUIRED"})
    with pytest.raises(JanusError, match="not approved"):
        janus.authorize(t.id)
    assert janus.get(t.id).state == State.PAYMENT_PENDING


# ---------- expiry ----------

def test_expired_authorization_is_never_captured(janus):
    t = authorized(janus)
    janus.paypal.expire(t.payment.authorization_id)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.EXPIRED and not t.payment.capture_id and not t.verdicts
    assert t.outcome.action == "expired"


def test_refresh_and_sweep_detect_expiry(janus):
    t = authorized(janus)
    janus.paypal.expire(t.payment.authorization_id)
    assert [x.state for x in janus.sweep()] == [State.EXPIRED]


def test_capture_rejected_as_expired(janus, monkeypatch):
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    monkeypatch.setattr(janus.paypal, "capture_authorization", lambda *a: (_ for _ in ()).throw(
        PayPalError("x", 422, body={"details": [{"issue": "AUTHORIZATION_EXPIRED"}]})))
    assert janus.release(t.id).state == State.EXPIRED


# ---------- failed captures ----------

def test_retryable_capture_failure_then_retry(janus, monkeypatch):
    t = authorized(janus)
    real = janus.paypal.capture_authorization
    monkeypatch.setattr(janus.paypal, "capture_authorization",
                        lambda *a: (_ for _ in ()).throw(PayPalError("network down", None)))
    with pytest.raises(PayPalError):
        janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    t = janus.get(t.id)
    assert t.state == State.CAPTURING and "network down" in t.payment.last_error
    assert t.last_verdict.action == "capture"  # verdict kept; not re-run
    monkeypatch.setattr(janus.paypal, "capture_authorization", real)
    t = janus.retry_settlement(t.id)
    assert t.state == State.CAPTURED and t.outcome.by == "janus" and t.payment.last_error is None


def test_declined_capture_goes_to_hold(tmp_path):
    from conftest import make_config
    from janus.service import Janus
    j = Janus(make_config(tmp_path, paypal_negative_test="capture=INSTRUMENT_DECLINED"))
    t = authorized(j)
    t = j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.HELD and "INSTRUMENT_DECLINED" in t.payment.last_error
    assert j.paypal.get_authorization(t.id, t.payment.authorization_id)["status"] == "CREATED"
    assert j.refund(t.id).state == State.VOIDED  # a human can still settle it


def test_capture_reconciles_when_already_captured(janus):
    t = authorized(janus)
    janus.paypal.capture_authorization(t.id, t.payment.authorization_id, f"janus-{t.id}-capture")  # never recorded
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.CAPTURED and t.payment.capture_id  # replayed, not double-captured


def test_void_after_outside_capture_reports_captured(janus):
    t = authorized(janus)
    janus.paypal.capture_authorization(t.id, t.payment.authorization_id, "someone-else")
    t = janus.submit_and_verify(t.id, PHOTOS / "received_mismatch.jpg")
    assert t.state == State.CAPTURED and t.outcome.by == "paypal"
