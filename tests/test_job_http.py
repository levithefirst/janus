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
    assert status == 200 and t["state"] == "partially_captured" and t["outcome"]["action"] == "partial"
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


# ---------- Approve PayPal hold: the first tap goes to PayPal, the second reuses the order ----------

def terms_of(call, m, tid, ct):
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    return re.search(r'name="terms_hash"\s+value="([0-9a-f]{64})"', html).group(1)


def tap(call, m, tid, ct):
    return call("POST", f"{m}/t/{tid}/pay?t={ct}", raw=f"terms_hash={terms_of(call, m, tid, ct)}".encode())


def test_first_tap_redirects_to_paypal_and_second_tap_reuses_the_order(env, monkeypatch):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    orders = []
    real = j.paypal.create_order
    monkeypatch.setattr(j.paypal, "create_order", lambda *a, **k: (orders.append(a[0]), real(*a, **k))[1])

    status, _, h1 = tap(call, m, tid, ct)
    t = j.get(tid)
    assert status == 303 and t.state.value == "payment_pending" and len(orders) == 1
    assert h1["Location"] == t.payment.approve_url and "/t/" not in h1["Location"]   # PayPal, not our own page
    assert "HttpOnly" in h1["Set-Cookie"]

    status, _, h2 = tap(call, m, tid, ct)                      # the buyer taps again before approving
    assert status == 303 and h2["Location"] == h1["Location"] and "HttpOnly" in h2["Set-Cookie"]
    assert len(orders) == 1 and j.get(tid).payment.order_id == t.payment.order_id   # no second order
    assert len(j.paypal._db()["orders"]) == 1


class FakeLinks:
    """What the create-order response looks like when it carries no approval link."""
    LINK = "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-X"


def test_missing_link_in_the_create_response_is_read_from_the_order(env, monkeypatch):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    real = j.paypal.create_order
    monkeypatch.setattr(j.paypal, "create_order", lambda *a, **k: dict(real(*a, **k), approve_url=None))
    gets = []
    monkeypatch.setattr(j.paypal, "get_order", lambda ref, oid: (gets.append(oid), {
        "id": oid, "status": "PAYER_ACTION_REQUIRED", "links": [{"rel": "payer-action", "href": FakeLinks.LINK}]})[1])
    status, _, h = tap(call, m, tid, ct)
    assert status == 303 and h["Location"] == FakeLinks.LINK and len(gets) == 1
    status, _, h = tap(call, m, tid, ct)                       # stored now: no further PayPal call
    assert status == 303 and h["Location"] == FakeLinks.LINK and len(gets) == 1


def test_no_link_anywhere_is_an_error_not_a_silent_bounce(env, monkeypatch):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    real = j.paypal.create_order
    monkeypatch.setattr(j.paypal, "create_order", lambda *a, **k: dict(real(*a, **k), approve_url=None))
    monkeypatch.setattr(j.paypal, "get_order", lambda ref, oid: {"id": oid, "status": "PAYER_ACTION_REQUIRED"})
    status, body, h = tap(call, m, tid, ct)
    assert status == 502 and "approval link" in body and "Location" not in h


def test_page_does_not_refresh_itself_while_the_form_is_submitting(env):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    assert "document.addEventListener('submit'" in html and "if(leaving)return" in html
    assert "Opening PayPal" in html


def test_first_tap_survives_the_live_refresh_in_a_real_browser(tmp_path):
    """Regression for the live bug: the response to the first tap arrives after the new state is saved, and the
    page's 4-second poll used to reload the page and drop the redirect. Skipped without node and playwright."""
    import json
    import shutil
    import subprocess
    import threading
    import time
    from http.server import ThreadingHTTPServer

    from conftest import ROOT
    from janus.security import RateLimiter
    from janus.server import make_handler
    node = shutil.which("node")
    if not node or subprocess.run([node, "-e", "require('playwright')"], capture_output=True).returncode != 0:
        pytest.skip("node with playwright is not available")
    j = Janus(make_config(tmp_path))
    Base = make_handler(j, RateLimiter({k: (9999, 60.0) for k in RateLimiter.LIMITS}))

    class Slow(Base):
        def _redirect(self, url, cookie=""):
            if "/pay" in self.path:
                time.sleep(6)       # state is saved; the redirect reaches the browser after a poll has fired
            return super()._redirect(url, cookie)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        j.config.__dict__  # frozen dataclass: the base URL must match the port, so build the job over HTTP
        base = f"http://127.0.0.1:{httpd.server_port}"
        import urllib.request
        object.__setattr__(j.config, "base_url", base)
        req = urllib.request.Request(base + "/api/jobs", data=json.dumps(
            {"title": "Fix tap", "price": "30", "description": "Replace the washer. Stop the drip."}).encode())
        link = json.loads(urllib.request.urlopen(req).read())["customer_link"]
        out = subprocess.run([node, str(ROOT / "tests" / "browser" / "first_tap.js"), link, "9000"], capture_output=True,
                             text=True, timeout=90)
        assert "STATE=authorized" in out.stdout, out.stdout + out.stderr
    finally:
        httpd.shutdown()


def test_lapsed_hold_customer_approves_a_new_hold_over_http(env):
    j, call, m = env
    tid, ct, pt, _ = create(call, m)
    approve_hold(call, m, tid, ct)
    photos(call, m, tid, pt)
    old = j.get(tid).payment.authorization_id
    j.paypal.expire(old)
    status, t, _ = call("POST", f"{m}/api/t/{tid}/refresh", {}, token=ct)
    assert status == 200 and t["state"] == "hold_lapsed"
    assert "Approve a new hold" not in call("GET", f"{m}/t/{tid}?k={pt}")[1]   # only the customer approves
    html = call("GET", f"{m}/t/{tid}?t={ct}")[1]
    assert "Approve a new hold" in html and "Your decision" not in html
    assert call("POST", f"{m}/api/t/{tid}/renew", {}, token=pt)[0] == 404
    terms = re.search(r'name="terms_hash"\s+value="([0-9a-f]{64})"', html).group(1)
    status, _, h = call("POST", f"{m}/t/{tid}/renew?t={ct}", raw=f"terms_hash={terms}".encode())
    assert status == 303 and f"Path={m}/paypal/return" in h["Set-Cookie"]
    ret = urlparse(h["Location"])
    assert parse_qs(ret.query)["token"][0] == j.get(tid).payment.renewal.order_id
    status, _, h = call("GET", f"{ret.path}?{ret.query}", cookie=h["Set-Cookie"].split(";")[0])
    assert status == 303 and h["Location"] == f"{m}/t/{tid}?t={ct}"
    t = j.get(tid)
    assert t.state.value == "packet_ready" and t.payment.previous_authorization_ids == [old]
    assert "Your decision" in call("GET", f"{m}/t/{tid}?t={ct}")[1]
    status, t, _ = call("POST", f"{m}/api/t/{tid}/accept", {}, token=ct)
    assert status == 200 and t["state"] == "captured" and len(t["payment"]["captures"]) == 1
