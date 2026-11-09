"""Per-job hash-chained audit trail: what it records, the read and verify endpoints, tamper detection."""
import json

from conftest import authorized, with_exif
from test_server import b64, create, pay, serve


def test_job_chain_records_paypal_calls_rule_checks_ai_and_user_actions(janus):
    t = authorized(janus)
    janus.submit_and_verify(t.id, with_exif("received_match.jpg"))
    kinds = [e["kind"] for e in janus.audit.entries(t.id)]
    for k in ("ai.compile", "consent", "paypal.create_order", "paypal.authorize", "evidence_added",
              "ai.verify", "verdict", "rule_check", "paypal.capture", "captured", "state.transition"):
        assert k in kinds, k
    seqs = [e["seq"] for e in janus.audit.entries(t.id)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all(e["threshold_id"] == t.id for e in janus.audit.entries(t.id))


def test_each_entry_links_to_the_previous_hash(janus):
    t = authorized(janus)
    e = janus.audit.entries(t.id)
    assert e[0]["prev"] == "0" * 64 and all(e[i]["prev"] == e[i - 1]["hash"] for i in range(1, len(e)))


def test_jobs_have_separate_chains(janus):
    a, b = authorized(janus), authorized(janus)
    assert janus.audit.entries(a.id)[0]["prev"] == janus.audit.entries(b.id)[0]["prev"] == "0" * 64
    assert not {x["hash"] for x in janus.audit.entries(a.id)} & {x["hash"] for x in janus.audit.entries(b.id)}


def test_tampering_with_a_job_chain_is_detected(janus):
    t = authorized(janus)
    p = janus.kv.local_dir / f"audit/{t.id}.jsonl"
    lines = p.read_text().splitlines()
    e = json.loads(lines[1]); e["reason"] = "rewritten"
    p.write_text("\n".join([lines[0], json.dumps(e), *lines[2:]]) + "\n")
    ok, msg = janus.audit.verify(t.id)
    assert not ok and "entry 2" in msg


def test_audit_endpoints(janus):
    httpd, call = serve(janus)
    try:
        tid, bt, st = create(call)
        pay(call, tid, bt)
        status, d, _ = call("GET", f"/api/t/{tid}/audit", token=bt)
        assert status == 200 and d["verified"] and d["count"] == len(d["entries"]) and d["head"]
        status, d2, _ = call("GET", f"/api/t/{tid}/audit?after={d['count'] - 1}", token=st)
        assert status == 200 and len(d2["entries"]) == 1
        status, v, _ = call("GET", f"/api/t/{tid}/audit/verify", token=bt)
        assert status == 200 and v["verified"] is True and v["head"] == d["head"]
        assert call("GET", f"/api/t/{tid}/audit")[0] == 404              # needs a token
        assert call("GET", f"/api/t/{tid}/audit/verify")[0] == 404
    finally:
        httpd.shutdown()
