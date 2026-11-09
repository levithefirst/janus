"""The two-sided job flow over HTTP, at the root and mounted under /app (as on Vercel)."""
import base64
import re
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import make_config
from janus.service import Janus
from test_jobs import after, before
from test_server import serve


def b64(data):
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


@pytest.fixture(params=["", "/app"])
def env(request, tmp_path):
    j = Janus(make_config(tmp_path, mount=request.param, base_url="http://localhost:8000" + request.param,
                          cron_secret="c" * 32))
    httpd, call = serve(j)
    yield j, call, request.param
    httpd.shutdown()


def create(call, m):
    status, d, _ = call("POST", f"{m}/api/jobs", {"title": "Patch wall hole", "price": "40.00",
                                                  "description": "Patch the hole. Paint the patch to match."})
    assert status == 201, d
    q = lambda u, k: parse_qs(urlparse(u).query)[k][0]  # noqa: E731
    return d["id"], q(d["customer_link"], "t"), q(d["provider_link"], "k"), d


def approve_hold(call, m, tid, ct):
    status, html, _ = call("GET", f"{m}/t/{tid}?t={ct}")
    assert status == 200 and "Approve PayPal hold" in html
    terms = re.search(r'name="terms_hash"\s+value="([0-9a-f]{64})"', html).group(1)
    assert f'action="{m}/t/{tid}/pay?t=' in html
    status, _, h = call("POST", f"{m}/t/{tid}/pay?t={ct}", raw=f"terms_hash={terms}".encode())
    assert status == 303 and f"Path={m}/paypal/return" in h["Set-Cookie"]
    ret = urlparse(h["Location"])
    assert ret.path == f"{m}/paypal/return"
    status, _, h = call("GET", f"{ret.path}?{ret.query}", cookie=h["Set-Cookie"].split(";")[0])
    assert status == 303 and h["Location"] == f"{m}/t/{tid}?t={ct}"


def photos(call, m, tid, pt, b=None, a=None):
    for phase, img in (("before", b or before()), ("after", a or after())):
        status, d, _ = call("POST", f"{m}/api/t/{tid}/photo", {"phase": phase, "photo_b64": b64(img)}, token=pt)
        assert status == 200, d
    status, d, _ = call("POST", f"{m}/api/t/{tid}/verify", {}, token=pt)
    assert status == 200, d
    return d


def test_good_job_partial_capture_then_void(env):
    j, call, m = env
    status, html, _ = call("GET", f"{m}/")
    assert status == 200 and "Write the checklist" in html
    tid, ct, pt, d = create(call, m)
    assert d["job"]["kind"] == "job" and d["job"]["compiled"]["requirements"]
    html = call("GET", f"{m}/t/{tid}?k={pt}")[1]
    assert "Send this to your customer" in html and "Approve PayPal hold" not in html
    approve_hold(call, m, tid, ct)
    t = photos(call, m, tid, pt)
    assert t["state"] == "packet_ready"
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    assert "Acceptance packet" in html and "Your decision" in html and "confidence" in html
    assert 'class="chip c-met">pass' in html
    assert "Your decision" not in call("GET", f"{m}/t/{tid}?k={pt}")[1]      # the provider cannot decide
    assert call("POST", f"{m}/api/t/{tid}/accept", {}, token=pt)[0] == 404   # not even through the API
    status, t, _ = call("POST", f"{m}/api/t/{tid}/partial", {"amount": "25.00"}, token=ct)
    assert status == 200 and t["state"] == "voided" and t["outcome"]["action"] == "partial"
    assert [c["amount"] for c in t["payment"]["captures"]] == ["25.00"]
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    assert "Partly paid" in html
    img = re.search(r'<img src="([^"]+)"', html).group(1).replace("&amp;", "&")
    assert img.startswith(f"{m}/t/{tid}/files/") and call("GET", img)[0] == 200


def test_reused_photo_job_flagged_blocked_then_rejected(env):
    j, call, m = env
    tid1, ct1, pt1, _ = create(call, m)
    approve_hold(call, m, tid1, ct1)
    reused = after(seed=21)
    photos(call, m, tid1, pt1, a=reused)
    tid, ct, pt, _ = create(call, m)
    approve_hold(call, m, tid, ct)
    t = photos(call, m, tid, pt, a=reused)
    assert t["state"] == "manual_review"
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    assert "Flagged" in html and "already used on another job" in html and 'id="ov"' in html
    status, d, _ = call("POST", f"{m}/api/t/{tid}/accept", {}, token=ct)
    assert status == 409 and "override" in d["error"]
    assert not j.get(tid).payment.captures
    status, t, _ = call("POST", f"{m}/api/t/{tid}/reject", {"reason": "photo is from another job"}, token=ct)
    assert status == 200 and t["state"] == "voided" and not t["payment"]["captures"]
    kinds = [e["kind"] for e in call("GET", f"{m}/api/t/{tid}/audit", token=ct)[1]["entries"]]
    assert "packet" in kinds and "customer_rejected" in kinds and "paypal.void" in kinds
    assert call("GET", f"{m}/api/t/{tid}/audit/verify", token=pt)[1]["verified"] is True


def test_override_is_recorded_over_http(env):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    approve_hold(call, m, tid, ct)
    b = before()
    photos(call, m, tid, pt, b=b, a=b)
    status, t, _ = call("POST", f"{m}/api/t/{tid}/accept", {"override_reason": "I saw it finished"}, token=ct)
    assert status == 200 and t["state"] == "captured"
    assert [c["reason"] for c in t["consents"] if c["kind"] == "override"] == ["I saw it finished"]


def test_cron_tick_without_the_secret_is_not_found(env):
    j, call, m = env
    assert call("GET", f"{m}/cron/tick")[0] == 404
    assert call("GET", f"{m}/cron/tick", token="c" * 32)[0] == 404  # the secret must come as a Bearer header


def test_cron_tick_runs_with_the_secret(tmp_path):
    import json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from janus.security import RateLimiter
    from janus.server import make_handler
    j = Janus(make_config(tmp_path, cron_secret="s" * 32))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(j, RateLimiter({k: (999, 60.0) for k in RateLimiter.LIMITS})))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/cron/tick"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + "s" * 32})
        assert json.loads(urllib.request.urlopen(req).read()) == {"actions": []}
        bad = urllib.request.Request(url, headers={"Authorization": "Bearer wrong"})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
    finally:
        httpd.shutdown()
