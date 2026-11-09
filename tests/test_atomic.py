"""Every transition is atomic: compare-and-set saves, one writer per threshold, terminal is frozen."""
import threading

import pytest

from conftest import PHOTOS, authorized, new_threshold
from janus.state import State
from janus.store import ConflictError, ImmutableError


def test_stale_write_is_rejected(janus):
    t = new_threshold(janus)
    a, b = janus.get(t.id), janus.get(t.id)
    janus.store.save(a)
    b.condition = "changed"
    with pytest.raises(ConflictError):
        janus.store.save(b)
    assert janus.get(t.id).condition != "changed"


def test_terminal_threshold_cannot_be_saved(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    assert t.state == State.CAPTURED
    for mutate in (lambda x: setattr(x, "state", State.VOIDED), lambda x: setattr(x, "condition", "x"),
                   lambda x: x.events.append({"a": 1})):
        fresh = janus.get(t.id)
        mutate(fresh)
        with pytest.raises(ImmutableError):
            janus.store.save(fresh)
    assert janus.get(t.id).state == State.CAPTURED


def test_concurrent_release_and_refund_settle_exactly_once(janus):
    """Buyer releases and seller refunds at the same moment: one wins, PayPal is called once."""
    t = authorized(janus)
    janus.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    calls = {"capture": 0, "void": 0}
    real_c, real_v = janus.paypal.capture_authorization, janus.paypal.void_authorization

    def cap(*a, **k):
        calls["capture"] += 1
        return real_c(*a, **k)

    def void(*a, **k):
        calls["void"] += 1
        return real_v(*a, **k)
    janus.paypal.capture_authorization, janus.paypal.void_authorization = cap, void
    errors = []

    def run(fn):
        try:
            fn(t.id)
        except Exception as e:  # the loser gets a TransitionError
            errors.append(type(e).__name__)
    ts = [threading.Thread(target=run, args=(f,)) for f in (janus.release, janus.refund) * 4]
    [x.start() for x in ts]; [x.join() for x in ts]
    assert calls["capture"] + calls["void"] == 1
    assert janus.get(t.id).state in (State.CAPTURED, State.VOIDED)
    assert errors and set(errors) == {"TransitionError"}
