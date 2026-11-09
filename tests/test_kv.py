"""The storage contract, run against every backend. Postgres runs when JANUS_TEST_DATABASE_URL is set."""
import os
import threading
import uuid

import pytest

from janus.kv import FileBackend, PostgresBackend, SqliteBackend

PG = os.environ.get("JANUS_TEST_DATABASE_URL")


def make(kind, tmp_path):
    if kind == "file":
        return FileBackend(tmp_path / "files")
    if kind == "sqlite":
        return SqliteBackend(tmp_path / "janus.db")
    if not PG:
        pytest.skip("JANUS_TEST_DATABASE_URL not set")
    return PostgresBackend(PG, schema=f"t_{uuid.uuid4().hex[:12]}")


KINDS = ["file", "sqlite", "postgres"]


@pytest.fixture(params=KINDS)
def kv(request, tmp_path):
    return make(request.param, tmp_path)


def test_blobs(kv):
    assert kv.get("a/b.jpg") is None
    kv.put("a/b.jpg", b"\x00\xffdata")
    assert kv.get("a/b.jpg") == b"\x00\xffdata"
    kv.put("a/b.jpg", b"new")
    assert kv.get("a/b.jpg") == b"new" and kv.keys("a/") == ["a/b.jpg"]
    kv.delete("a/b.jpg")
    assert kv.get("a/b.jpg") is None


def test_add_is_create_only(kv):
    assert kv.add("webhooks/WH-1") is True and kv.add("webhooks/WH-1") is False


def test_cas_doc(kv):
    assert kv.get_doc("thresholds/x/doc.json") == (0, None)
    assert kv.cas_doc("thresholds/x/doc.json", 0, b"v1")
    assert not kv.cas_doc("thresholds/x/doc.json", 0, b"again")        # already exists
    assert kv.cas_doc("thresholds/x/doc.json", 1, b"v2")
    assert not kv.cas_doc("thresholds/x/doc.json", 1, b"stale")         # stale version
    assert kv.get_doc("thresholds/x/doc.json") == (2, b"v2")
    kv.cas_doc("thresholds/y/doc.json", 0, b"other")
    assert sorted(kv.docs("thresholds/")) == [b"other", b"v2"]


def test_append_builds_on_the_last_entry(kv):
    for _ in range(3):
        kv.append("s", lambda last: {"n": (last["n"] + 1) if last else 1})
    assert [e["n"] for e in kv.stream("s")] == [1, 2, 3] and kv.stream("other") == []


def test_concurrent_appends_form_one_chain(kv):
    def work():
        for _ in range(10):
            kv.append("c", lambda last: {"n": (last["n"] + 1) if last else 1})
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert [e["n"] for e in kv.stream("c")] == list(range(1, 41))


def test_concurrent_cas_has_one_winner(kv):
    kv.cas_doc("d", 0, b"0")
    wins = []

    def race():
        if kv.cas_doc("d", 1, b"mine"):
            wins.append(1)
    ts = [threading.Thread(target=race) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(wins) == 1 and kv.get_doc("d")[0] == 2


def test_lock_is_exclusive_and_reentrant(kv):
    order = []

    def hold():
        with kv.lock("job"):
            order.append("b-in")
    with kv.lock("job"):
        with kv.lock("job"):  # same thread re-enters
            t = threading.Thread(target=hold)
            t.start()
            t.join(0.3)
            order.append("a-done")
    t.join(5)
    assert order == ["a-done", "b-in"]


def test_bad_keys_refused(kv):
    for key in ("", "/etc/passwd", "a/../../x"):
        with pytest.raises(ValueError):
            kv.put(key, b"x")
