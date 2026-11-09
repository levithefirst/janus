"""The loop, end to end, on every storage backend: settlement, races, webhooks, audit chains, photos."""
import json
import os
import threading
import uuid

import pytest

from conftest import PHOTOS, authorized, make_config, with_exif
from janus.service import Janus
from janus.state import State
from janus.webhooks import handle

PG = os.environ.get("JANUS_TEST_DATABASE_URL")


@pytest.fixture(params=["file", "sqlite", "postgres"])
def j(request, tmp_path, monkeypatch):
    kind = request.param
    if kind == "postgres":
        if not PG:
            pytest.skip("JANUS_TEST_DATABASE_URL not set")
        from janus import kv
        schema = f"t_{uuid.uuid4().hex[:12]}"
        monkeypatch.setattr(kv, "from_config", lambda c: kv.PostgresBackend(PG, schema=schema))
        return Janus(make_config(tmp_path, store="postgres", database_url=PG))
    return Janus(make_config(tmp_path, store=kind))


def test_match_captures_and_photos_round_trip(j):
    t = authorized(j)
    t = j.submit_and_verify(t.id, with_exif("received_match.jpg"))
    assert t.state == State.CAPTURED and len(t.payment.captures) == 1
    assert j.store.get_file(t.id, t.evidence[0].file)[:3] == b"\xff\xd8\xff"
    assert j.store.get_file(t.id, "../../etc/passwd") is None


def test_mismatch_voids_and_reuse_is_caught_across_jobs(j):
    a = authorized(j)
    assert j.submit_and_verify(a.id, PHOTOS / "received_mismatch.jpg").state == State.VOIDED
    b = authorized(j)
    t = j.submit_and_verify(b.id, PHOTOS / "received_mismatch.jpg")
    assert t.state == State.HELD and any(f.startswith("reused_photo") for f in t.evidence[-1].flags)


def test_release_and_refund_race_settles_once(j):
    t = authorized(j)
    j.submit_and_verify(t.id, PHOTOS / "received_unclear.jpg")
    ths = [threading.Thread(target=lambda f=f: _try(f, t.id)) for f in (j.release, j.refund) * 3]
    [x.start() for x in ths]; [x.join() for x in ths]
    x = j.get(t.id)
    assert x.state in (State.CAPTURED, State.VOIDED)
    db = j.paypal._db()
    assert len(db["captures"]) + sum(a["status"] == "VOIDED" for a in db["auths"].values()) == 1


def _try(fn, tid):
    try:
        fn(tid)
    except Exception:
        pass


def test_webhook_dedupe(j):
    t = authorized(j)
    body = json.dumps({"id": "WH-1", "event_type": "X", "resource": {"id": t.payment.authorization_id}}).encode()
    h = j.paypal.sign_webhook(body, j.config.paypal_webhook_id)
    assert handle(j, h, body)[0] == 200 and handle(j, h, body)[1]["duplicate"] is True


def test_per_job_audit_chain_verifies(j):
    t = authorized(j)
    j.submit_and_verify(t.id, with_exif("received_match.jpg"))
    assert j.audit.verify(t.id)[0] and j.audit.verify()[0]
    kinds = {e["kind"] for e in j.audit.entries(t.id)}
    assert {"consent", "verdict", "captured"} <= kinds
