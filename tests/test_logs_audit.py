import json
import threading

from janus.audit import AuditLog
from janus.logs import log_event, redact, setup


def test_redacts_pii_and_secrets():
    r = redact({"note": "call me", "payer": {"email": "a@b.com"}, "seller_key": "abc",
                "msg": "mail jo.doe+x@example.co.uk or +1 (415) 555-0100, Bearer eyJ.abc, /t/th_1?k=SECRET&x=1",
                "gps": [51.5, -0.1], "amount": "24.00", "authorization_id": "AUTH1"})
    s = json.dumps(r)
    for leak in ("call me", "a@b.com", "abc\"", "jo.doe", "555-0100", "eyJ", "SECRET", "51.5"):
        assert leak not in s, leak
    assert r["amount"] == "24.00" and r["authorization_id"] == "AUTH1" and "x=1" in r["msg"]


def test_log_file_is_redacted(tmp_path):
    setup(tmp_path)
    log_event("security", "probe", "th_1", note="secret note", email="x@y.com")
    text = (tmp_path / "janus.log.jsonl").read_text()
    assert "secret note" not in text and "x@y.com" not in text


def test_audit_chain_and_tamper_detection(tmp_path):
    a = AuditLog(tmp_path)
    for i in range(3):
        a.append("verdict", "th_1", "janus", f"reason {i}", confidence=0.9)
    assert a.verify() == (True, "3 entries, chain intact")
    lines = a.path.read_text().splitlines()
    e = json.loads(lines[1]); e["reason"] = "edited"
    a.path.write_text("\n".join([lines[0], json.dumps(e), lines[2]]) + "\n")
    ok, msg = a.verify()
    assert not ok and "line 2" in msg


def test_audit_deletion_detected(tmp_path):
    a = AuditLog(tmp_path)
    for i in range(3):
        a.append("verdict", "th_1", "janus", str(i))
    lines = a.path.read_text().splitlines()
    a.path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not a.verify()[0]


def test_audit_concurrent_appends_keep_one_chain(tmp_path):
    a = AuditLog(tmp_path)
    ts = [threading.Thread(target=lambda: [a.append("x", None, "t", "r") for _ in range(20)]) for _ in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert a.verify() == (True, "100 entries, chain intact")
