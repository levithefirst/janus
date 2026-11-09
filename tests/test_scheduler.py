"""The expiry timeline, driven by a frozen UTC clock."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import PHOTOS, authorized, make_config
from janus import clock
from janus.scheduler import tick
from janus.service import Janus
from janus.state import State

T0 = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)


def acts(j):
    return [a["action"] for a in tick(j)]


@pytest.fixture
def setup(tmp_path):
    with clock.frozen(T0) as travel:
        j = Janus(make_config(tmp_path))
        t = authorized(j)
        yield j, t, travel


def test_timeline_warn_reauthorize_ask_void(setup):
    j, t, travel = setup
    assert j.get(t.id).payment.authorized_at == clock.iso(T0)
    travel.advance(timedelta(days=2))
    assert acts(j) == []
    travel.advance(timedelta(hours=12))                      # day 2.5
    assert acts(j) == ["warned"] and acts(j) == []           # once only
    travel.advance(timedelta(hours=12))                      # day 3
    assert acts(j) == ["reauthorized"]
    x = j.get(t.id)
    assert x.payment.previous_authorization_ids == [t.payment.authorization_id]
    assert x.payment.original_authorized_at == clock.iso(T0)   # the 29-day clock is not reset
    travel.advance(timedelta(days=3))                        # day 6: second honor period over
    assert acts(j) == ["new_authorization_requested"] and acts(j) == []
    assert j.get(t.id).payment.needs_new_authorization
    travel.advance(timedelta(days=21, hours=23))             # just before day 28
    assert acts(j) == []
    travel.advance(timedelta(hours=1))                       # day 28
    assert acts(j) == ["voided_before_expiry"]
    x = j.get(t.id)
    assert x.state == State.VOIDED and x.outcome.by == "scheduler" and not x.payment.captures
    kinds = [e["kind"] for e in j.audit.entries(t.id)]
    for k in ("honor_period_warning", "reauthorized", "new_authorization_needed", "void_requested", "voided"):
        assert k in kinds


def test_silent_customer_is_never_captured(setup):
    """Evidence says met but the customer only consented to auto-capture on their own evidence and
    then went silent. Over the whole 29 days the scheduler never captures."""
    j, t, travel = setup
    j.submit_evidence(t.id, PHOTOS / "received_unclear.jpg")
    j.verify(t.id)
    for _ in range(40):
        travel.advance(timedelta(hours=18))
        tick(j)
    x = j.get(t.id)
    assert x.state == State.VOIDED and not x.payment.captures
    assert all(e["actor"] != "scheduler" or "capture" not in e["kind"] for e in j.audit.entries(t.id))


def test_open_dispute_is_voided_before_expiry(setup):
    j, t, travel = setup
    j.dispute(t.id, "not what I ordered")
    travel.advance(timedelta(days=28))
    tick(j)
    assert j.get(t.id).state == State.VOIDED


def test_reauthorize_rejected_asks_for_new_authorization(tmp_path):
    with clock.frozen(T0) as travel:
        j = Janus(make_config(tmp_path, paypal_negative_test="reauthorize=REAUTHORIZATION_NOT_ALLOWED"))
        t = authorized(j)
        travel.advance(timedelta(days=3, minutes=1))
        assert acts(j) == ["new_authorization_requested"]
        assert j.get(t.id).payment.authorization_id == t.payment.authorization_id


def test_paypal_expiry_earlier_than_ours_wins(setup):
    j, t, travel = setup
    with j.store.lock(t.id):
        x = j.get(t.id)
        x.payment.expiration_time = clock.iso(T0 + timedelta(days=10))
        j.store.save(x)
    travel.advance(timedelta(days=9, hours=1))
    assert "voided_before_expiry" in acts(j)


def test_capture_in_flight_is_not_touched_by_expiry_but_recovered(setup, monkeypatch):
    j, t, travel = setup
    monkeypatch.setattr(j.paypal, "capture_authorization", lambda *a, **k: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        j.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    monkeypatch.undo()
    assert acts(j) == ["recovered:captured"]
