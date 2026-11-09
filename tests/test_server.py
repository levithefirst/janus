"""Drive the full loop through the HTTP API, including the customer link redirect and PayPal return URL."""
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import EXAMPLES
from eid.server import make_handler


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def api(eid):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(eid))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    opener = urllib.request.build_opener(NoRedirect)

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            r = opener.open(req)
        except urllib.error.HTTPError as e:
            r = e
        raw = r.read().decode()
        try:
            return r.status, json.loads(raw), r.headers
        except json.JSONDecodeError:
            return r.status, raw, r.headers
    yield call
    httpd.shutdown()


def test_http_flow(api, job_text):
    status, job, _ = api("POST", "/jobs", {"text": job_text})
    assert status == 201
    jid = job["id"]
    status, err, _ = api("POST", f"/jobs/{jid}/approve")
    assert status == 409 and "cannot go" in err["error"]
    status, _, headers = api("GET", f"/jobs/{jid}/pay")       # customer opens their link
    assert status == 302
    approve_url = headers["Location"]
    order_id = approve_url.split("token=")[1]
    status, text, _ = api("GET", f"/paypal/return?token={order_id}")  # PayPal sends the customer back
    assert status == 200 and "authorized" in text
    items = json.loads((EXAMPLES / "evidence_complete.json").read_text())["items"]
    for it in items:
        it["file"] = str(EXAMPLES / it["file"])
    assert api("POST", f"/jobs/{jid}/evidence", {"items": items})[1]["state"] == "evidence_submitted"
    assert api("POST", f"/jobs/{jid}/packet")[1]["state"] == "packet_ready"
    status, packet, _ = api("GET", f"/jobs/{jid}/packet")
    assert status == 200 and packet.startswith("# Acceptance packet")
    status, job, _ = api("POST", f"/jobs/{jid}/approve", {"note": "great"})
    assert status == 200 and job["state"] == "captured"
