"""Re-payment: a job whose hold lapsed (or could not be reauthorized) gets a fresh AUTHORIZE for the same job.

The money rules that matter here: never two captures across the old and the new authorization, never a
capture on a replaced authorization, and a late capture on the old hold always wins over the new hold.
"""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_config
from janus import clock
from janus.paypal import PayPalError
from janus.rules import RuleViolation, check_capture
from janus.scheduler import tick
from janus.service import Janus, JanusError, StaleTermsError
from janus.state import State, TransitionError
from janus.store import Pending
from test_jobs import job, packet

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def j(tmp_path):
    return Janus(make_config(tmp_path))


def kinds(j, t):
    return [e["kind"] for e in j.audit.entries(t.id)]


def lapse_by_expiry(j):
    t = job(j)
    t = packet(j, t)
    before_state = t.state
    j.paypal.expire(t.payment.authorization_id)
    t = j.refresh(t.id)
    return t, before_state


def renew(j, t):
    t = j.start_renewal(t.id, channel="api")
    return j.authorize(t.id, order_id=t.payment.renewal.order_id)


def captures(j):
    return list(j.paypal._db()["captures"].values())


# ---------- the hold lapses instead of closing the job ----------

def test_expired_hold_lapses_and_keeps_the_job(j):
    t, was = lapse_by_expiry(j)
    assert t.state == State.HOLD_LAPSED and t.resume_state == was and t.outcome is None
    assert t.payment.authorization_status == "EXPIRED" and len(t.evidence) == 2 and t.verdicts
    with pytest.raises(TransitionError):
        j.accept(t.id)                                        # nothing is held: nothing can be captured
    with pytest.raises(TransitionError):
        j.submit_evidence(t.id, b"x", by="seller", phase="after")
    assert "hold_lapsed" in kinds(j, t) and not captures(j)


def test_customer_approves_a_new_hold_for_the_same_job(j):
    t, was = lapse_by_expiry(j)
    old_order, old_auth, checklist = t.payment.order_id, t.payment.authorization_id, t.compiled
    t = j.start_renewal(t.id, channel="api")
    r = t.payment.renewal
    assert r.status == "pending" and r.order_id != old_order and r.replaced_authorization_id == old_auth
    assert j.start_renewal(t.id).payment.renewal.order_id == r.order_id   # idempotent: one open order
    t = j.authorize(t.id, order_id=r.order_id)
    p = t.payment
    assert t.state == was and t.resume_state is None and t.compiled == checklist and len(t.evidence) == 2
    assert p.order_id == r.order_id and p.previous_order_ids == [old_order]
    assert p.authorization_id != old_auth and p.previous_authorization_ids == [old_auth]
    assert p.renewal.status == "done" and p.renewal.new_authorization_id == p.authorization_id
    assert j.paypal.auth(old_auth)["status"] == "EXPIRED" and j.paypal.auth(p.authorization_id)["status"] == "CREATED"
    k = kinds(j, t)
    assert k.index("hold_lapsed") < k.index("renewal_requested") < k.index("renewed")
    assert j.audit.verify(t.id)[0]
    t = j.accept(t.id, override_reason="checked it myself")
    assert t.state == State.CAPTURED
    [c] = captures(j)
    assert c["authorization_id"] == p.authorization_id and t.payment.captures[0].authorization_id == p.authorization_id
    assert j.paypal.auth(old_auth)["captured"] == "0.00"


def test_an_old_order_link_never_readopts_the_lapsed_authorization(j):
    t, _ = lapse_by_expiry(j)
    assert j.authorize(t.id, order_id=t.payment.order_id).state == State.HOLD_LAPSED
    assert j.authorize(t.id).state == State.HOLD_LAPSED     # no renewal pending: nothing to authorize


def test_renewal_is_refused_on_stale_terms_and_when_the_hold_is_fine(j):
    t = job(j)
    with pytest.raises(TransitionError):
        j.start_renewal(t.id)                                 # the hold is valid: no new hold is needed
    j.paypal.expire(t.payment.authorization_id)
    j.refresh(t.id)
    with pytest.raises(StaleTermsError):
        j.start_renewal(t.id, terms_hash="0" * 64)
    assert j.get(t.id).payment.renewal is None


def test_customer_accept_on_an_expired_hold_lapses_and_captures_nothing(j):
    t = packet(j, job(j))
    was = t.state
    j.paypal.expire(t.payment.authorization_id)
    t = j.accept(t.id, override_reason="fine")
    assert t.state == State.HOLD_LAPSED and t.resume_state == was and not t.payment.captures and not captures(j)
    t = renew(j, t)
    assert t.state == was and t.payment.renewal.status == "done"


def test_reject_while_lapsed_closes_the_job_without_paypal(j, monkeypatch):
    t, _ = lapse_by_expiry(j)
    monkeypatch.setattr(j.paypal, "void_authorization", lambda *a, **k: pytest.fail("no hold to void"))
    t = j.reject(t.id, "changed my mind")
    assert t.state == State.VOIDED and "nothing was charged" in t.outcome.reason


# ---------- the scheduler releases the hold before expiry; a new hold gets its own request ids ----------

def test_scheduler_void_lapses_then_new_hold_has_its_own_request_ids(tmp_path):
    with clock.frozen(T0) as travel:
        j = Janus(make_config(tmp_path))
        t = packet(j, job(j))
        old = t.payment.authorization_id
        travel.advance(timedelta(days=27, hours=1))
        acts = [a["action"] for a in tick(j)]
        assert "voided_before_expiry" in acts
        t = j.get(t.id)
        assert t.state == State.HOLD_LAPSED and j.paypal.auth(old)["status"] == "VOIDED"
        t = renew(j, t)
        new = t.payment.authorization_id
        assert t.payment.authorized_at == clock.iso() == t.payment.original_authorized_at  # a fresh 29-day clock
        t = j.reject(t.id, "not needed any more")
        # The old void used janus-<id>-void. Reusing it would make PayPal replay that void and leave the
        # new hold open; the new hold's void has its own id and really releases it.
        assert t.state == State.VOIDED and j.paypal.auth(new)["status"] == "VOIDED"
        assert t.outcome.by == "buyer"


# ---------- reauthorization was not possible: renew while the old hold is still open ----------

def needs_new(j, t):
    with j._locked(t.id) as x:
        x.payment.needs_new_authorization = True
        j.store.save(x)
    return j.get(t.id)


def test_renewal_while_the_old_hold_is_open_voids_the_old_one(j):
    t = needs_new(j, packet(j, job(j)))
    old, state = t.payment.authorization_id, t.state
    t = renew(j, t)
    assert t.state == state and not t.payment.needs_new_authorization
    assert j.paypal.auth(old)["status"] == "VOIDED" and t.payment.previous_authorization_ids == [old]
    assert j.accept(t.id, override_reason="ok").state == State.CAPTURED
    [c] = captures(j)
    assert c["authorization_id"] == t.payment.authorization_id


# ---------- races between a late capture on the old hold and the new hold ----------

def test_late_capture_on_old_hold_before_return_means_no_new_hold(j):
    """The old hold was captured on PayPal (late) while the customer was approving the new one."""
    t = needs_new(j, packet(j, job(j)))
    old = t.payment.authorization_id
    t = j.start_renewal(t.id)
    order = t.payment.renewal.order_id
    j.paypal.capture_authorization(t.id, old, "outside-janus", amount=t.amount, final_capture=True)
    t = j.authorize(t.id, order_id=order)
    assert t.state == State.CAPTURED and "alert" in kinds(j, t)
    assert j.paypal._db()["orders"][order]["auth_id"] is None   # the new order was never authorized
    assert len(captures(j)) == 1
    assert j.authorize(t.id, order_id=order).state == State.CAPTURED   # a second return changes nothing


def test_late_capture_on_old_hold_after_new_auth_voids_the_new_hold(j, monkeypatch):
    """The capture on the old hold lands between authorizing the new hold and voiding the old one."""
    t = needs_new(j, packet(j, job(j)))
    old = t.payment.authorization_id
    real = j.paypal.authorize_order

    def authorize_then_late_capture(ref, order_id, request_id=None):
        out = real(ref, order_id, request_id)
        j.paypal.capture_authorization(ref, old, "late-capture", amount=t.amount, final_capture=True)
        return out
    monkeypatch.setattr(j.paypal, "authorize_order", authorize_then_late_capture)
    t = renew(j, t)
    new = t.payment.renewal.new_authorization_id
    assert t.state == State.CAPTURED and t.payment.renewal.status == "failed"
    assert j.paypal.auth(new)["status"] == "VOIDED"                 # the second hold was released at once
    assert len(captures(j)) == 1 and "renewal_failed" in kinds(j, t)


def test_capture_in_flight_on_old_hold_blocks_the_renewal_then_wins(j, monkeypatch):
    """The customer accepts on the old hold, PayPal fails retryably, and the renewal return arrives."""
    t = needs_new(j, packet(j, job(j)))
    t = j.start_renewal(t.id)
    order = t.payment.renewal.order_id

    def flaky(*a, **k):
        raise PayPalError("PayPal: HTTP 500 INTERNAL_SERVER_ERROR", 500,
                          body={"name": "INTERNAL_SERVER_ERROR", "details": [{"issue": "INTERNAL_SERVER_ERROR"}]})
    real = j.paypal.capture_authorization
    monkeypatch.setattr(j.paypal, "capture_authorization", flaky)
    with pytest.raises(PayPalError):
        j.accept(t.id, override_reason="ok")
    assert j.get(t.id).state == State.CAPTURING
    with pytest.raises(TransitionError):
        j.authorize(t.id, order_id=order)                      # never a second hold beside a capture in flight
    monkeypatch.setattr(j.paypal, "capture_authorization", real)
    assert j.retry_settlement(t.id).state == State.CAPTURED
    assert j.authorize(t.id, order_id=order).state == State.CAPTURED
    assert j.paypal._db()["orders"][order]["auth_id"] is None and len(captures(j)) == 1


def test_rules_block_a_capture_written_for_the_replaced_authorization(j):
    t, _ = lapse_by_expiry(j)
    old = t.payment.authorization_id
    t = renew(j, t)
    t.pending = Pending(action="capture", by="buyer", reason="replayed", request_id="x", amount=t.amount,
                        authorization_id=old, at=clock.iso())
    with pytest.raises(RuleViolation) as ex:
        check_capture(t, "buyer", 0.85)
    assert ex.value.rule == "stale_authorization"
    t.pending.authorization_id = t.payment.authorization_id
    t.payment.authorization_id = old                           # pointed back at a replaced authorization
    with pytest.raises(RuleViolation) as ex:
        check_capture(t, "buyer", 0.85)
    assert ex.value.rule == "stale_authorization"


def test_rules_block_capturing_more_than_the_price_across_authorizations(j):
    from janus.store import Capture
    t, _ = lapse_by_expiry(j)
    t = renew(j, t)
    t.payment.captures.append(Capture(capture_id="C-old", authorization_id=t.payment.previous_authorization_ids[0],
                                      amount="30.00", request_id="r", final=False, at=clock.iso()))
    t.pending = Pending(action="capture", by="buyer", reason="r", request_id="y", amount="20.00",
                        authorization_id=t.payment.authorization_id, at=clock.iso())
    with pytest.raises(RuleViolation) as ex:
        check_capture(t, "buyer", 0.85)
    assert ex.value.rule == "over_capture"


def test_threshold_mode_still_expires(tmp_path):
    from conftest import authorized
    j = Janus(make_config(tmp_path))
    t = authorized(j)
    j.paypal.expire(t.payment.authorization_id)
    assert j.refresh(t.id).state == State.EXPIRED
    with pytest.raises((JanusError, RuleViolation)):
        j.start_renewal(t.id)
