"""Webhooks: signature checked, deduplicated by event id, order-independent, and never able to capture."""
import json
import threading

import pytest

from conftest import PHOTOS, authorized
from janus.state import State
from janus.webhooks import handle


def deliver(j, event, sign=True, event_id=None):
    event = dict(event, id=event_id or event.get("id", "WH-1"))
    body = json.dumps(event).encode()
    headers = j.paypal.sign_webhook(body, j.config.paypal_webhook_id) if sign else {}
    return handle(j, headers, body)


def ev(etype, t, eid):
    return {"id": eid, "event_type": etype, "resource": {
        "id": t.payment.authorization_id, "custom_id": t.id,
        "supplementary_data": {"related_ids": {"order_id": t.payment.order_id}}}}


def test_bad_signature_rejected_and_audited(janus):
    t = authorized(janus)
    status, _ = deliver(janus, ev("PAYMENT.AUTHORIZATION.VOIDED", t, "WH-1"), sign=False)
    assert status == 401 and janus.get(t.id).state == State.AUTHORIZED
    body = json.dumps(ev("PAYMENT.AUTHORIZATION.VOIDED", t, "WH-2")).encode()
    headers = janus.paypal.sign_webhook(body, janus.config.paypal_webhook_id)
    assert handle(janus, headers, body.replace(b"VOIDED", b"CREATED"))[0] == 401  # tampered body
    assert any(e["kind"] == "webhook_rejected" for e in janus.audit.entries())


def test_duplicates_are_dropped(janus):
    t = authorized(janus)
    first = deliver(janus, ev("PAYMENT.AUTHORIZATION.CREATED", t, "WH-9"))
    second = deliver(janus, ev("PAYMENT.AUTHORIZATION.CREATED", t, "WH-9"))
    assert first[0] == second[0] == 200 and second[1]["duplicate"] is True
    assert sum(e["kind"] == "webhook" for e in janus.audit.entries(t.id)) == 1


def test_concurrent_duplicates_processed_once(janus):
    t = authorized(janus)
    results = []
    ths = [threading.Thread(target=lambda: results.append(deliver(janus, ev("X", t, "WH-C")))) for _ in range(8)]
    [x.start() for x in ths]; [x.join() for x in ths]
    assert sum(1 for _, r in results if not r.get("duplicate")) == 1


def test_event_cannot_capture_by_claiming_it(janus):
    """A (validly signed) event says the capture completed. PayPal says CREATED. Nothing moves."""
    t = authorized(janus)
    _, out = deliver(janus, ev("PAYMENT.CAPTURE.COMPLETED", t, "WH-3"))
    assert out["state"] == "authorized" and not janus.get(t.id).payment.captures


@pytest.mark.parametrize("order", [("PAYMENT.AUTHORIZATION.CREATED", "PAYMENT.AUTHORIZATION.VOIDED"),
                                   ("PAYMENT.AUTHORIZATION.VOIDED", "PAYMENT.AUTHORIZATION.CREATED")])
def test_out_of_order_events_converge(janus, order):
    """The authorization was voided on PayPal's side. Whatever order the events arrive in, we end voided."""
    t = authorized(janus)
    janus.paypal.void_authorization(t.id, t.payment.authorization_id, "outside-janus")
    for i, etype in enumerate(order):
        deliver(janus, ev(etype, t, f"WH-{i}"))
    t = janus.get(t.id)
    assert t.state == State.VOIDED and t.outcome.by == "paypal"


def test_late_event_after_terminal_is_ignored(janus):
    t = authorized(janus)
    t = janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    v = t.version
    _, out = deliver(janus, ev("PAYMENT.AUTHORIZATION.CREATED", t, "WH-late"))
    assert out["state"] == "captured" and janus.get(t.id).version == v


def test_webhook_completes_an_interrupted_capture(janus, monkeypatch):
    t = authorized(janus)
    real = janus.paypal.capture_authorization
    monkeypatch.setattr(janus.paypal, "capture_authorization",
                        lambda *a, **k: (real(*a, **k), (_ for _ in ()).throw(SystemExit()))[1])
    with pytest.raises(SystemExit):
        janus.submit_and_verify(t.id, PHOTOS / "received_match.jpg")
    monkeypatch.setattr(janus.paypal, "capture_authorization", real)
    _, out = deliver(janus, ev("PAYMENT.CAPTURE.COMPLETED", janus.get(t.id), "WH-cap"))
    assert out["state"] == "captured" and len(janus.get(t.id).payment.captures) == 1


def test_unmatched_event(janus):
    status, out = deliver(janus, {"id": "WH-x", "event_type": "X", "resource": {"id": "nope"}})
    assert status == 200 and out["matched"] is False


def test_failed_processing_releases_the_claim(janus, monkeypatch):
    t = authorized(janus)
    monkeypatch.setattr(janus, "reconcile", lambda tid: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        deliver(janus, ev("X", t, "WH-r"))
    monkeypatch.undo()
    assert deliver(janus, ev("X", t, "WH-r"))[1].get("duplicate") is None  # PayPal's retry is processed
