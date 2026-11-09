"""The loop through HTTP, with capability tokens: create, buyer link -> PayPal -> return URL, upload, outcome."""
import base64
import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import LISTING, PHOTOS, with_exif
from janus.security import RateLimiter
from janus.server import make_handler


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def serve(janus, limiter=None):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(janus, limiter or RateLimiter(
        {k: (10_000, 60.0) for k in RateLimiter.LIMITS})))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    opener = urllib.request.build_opener(NoRedirect)

    def call(method, path, body=None, token=None, raw=None, cookie=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Janus-Token"] = token
        if cookie:
            headers["Cookie"] = cookie
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(base + path, method=method, data=data, headers=headers)
        try:
            r = opener.open(req)
        except urllib.error.HTTPError as e:
            r = e
        out = r.read()
        try:
            return r.status, json.loads(out), r.headers
        except (json.JSONDecodeError, UnicodeDecodeError):
            return r.status, out.decode(errors="replace"), r.headers
    return httpd, call


@pytest.fixture
def api(janus):
    httpd, call = serve(janus)
    yield call
    httpd.shutdown()


def b64(data):
    data = data if isinstance(data, bytes) else (PHOTOS / data).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode()


def tokens(d):
    return parse_qs(urlparse(d["buyer_link"]).query)["t"][0], parse_qs(urlparse(d["seller_link"]).query)["k"][0]


def create(api):
    status, d, _ = api("POST", "/api/thresholds", LISTING | {"photo_b64": b64("listing_red_mug.jpg")})
    assert status == 201
    return d["id"], *tokens(d)


def shown_terms_hash(api, tid, bt):
    """The terms hash the buyer's page puts in its Authorize form, as a browser would submit it."""
    status, html, _ = api("GET", f"/t/{tid}?t={bt}")
    assert status == 200
    return re.search(r'name="terms_hash"\s+value="([0-9a-f]{64})"', html).group(1)


def post_pay(api, tid, bt, terms_hash):
    return api("POST", f"/t/{tid}/pay?t={bt}", raw=f"terms_hash={terms_hash}".encode())


def pay(api, tid, bt):
    status, _, h = post_pay(api, tid, bt, shown_terms_hash(api, tid, bt))  # buyer taps "Authorize with PayPal"
    assert status == 303 and "HttpOnly" in h["Set-Cookie"] and "SameSite=Lax" in h["Set-Cookie"]
    cookie = h["Set-Cookie"].split(";")[0]
    approve = urlparse(h["Location"])
    assert bt not in h["Location"]                          # the token never goes to PayPal
    status, _, h = api("GET", f"{approve.path}?{approve.query}", cookie=cookie)  # PayPal sends the buyer back
    assert status == 303 and h["Location"] == f"/t/{tid}?t={bt}"


def test_get_pay_records_nothing(api, janus):
    """Link previewers and prefetchers issue GETs: a GET must not consent or create an order."""
    tid, bt, st = create(api)
    for _ in range(3):
        status, _, h = api("GET", f"/t/{tid}/pay?t={bt}")
        assert status == 303 and h["Location"].startswith(f"http://localhost:8000/t/{tid}") and "Set-Cookie" not in h
    api("GET", f"/t/{tid}?t={bt}")
    t = janus.get(tid)
    assert t.consents == [] and t.payment.order_id is None and t.state.value == "created"
    assert not any(e["kind"] == "consent" for e in janus.audit.entries(tid))
    html = api("GET", f"/t/{tid}?t={bt}")[1]
    assert "Authorize with PayPal" in html and 'method="post"' in html and 'name="terms_hash"' in html


def test_post_pay_records_consent_bound_to_terms(api, janus):
    tid, bt, st = create(api)
    shown = shown_terms_hash(api, tid, bt)
    status, _, h = post_pay(api, tid, bt, shown)
    assert status == 303 and "HttpOnly" in h["Set-Cookie"] and h["Location"].startswith("http://localhost:8000/paypal/return")
    t = janus.get(tid)
    assert [c.kind for c in t.consents] == ["auto_capture_on_verified"] and t.consents[0].terms_hash == shown
    assert t.consents[0].channel == "web" and t.state.value == "payment_pending"
    post_pay(api, tid, bt, shown)   # pressing it twice records nothing more
    assert len(janus.get(tid).consents) == 1


def test_post_pay_with_stale_terms_is_rejected(api, janus):
    tid, bt, st = create(api)
    shown = shown_terms_hash(api, tid, bt)
    assert api("POST", f"/api/t/{tid}/amend", {"price": "20.00", "reason": "x"}, token=st)[0] == 200
    janus.accept_amendment(tid)          # terms change after the buyer loaded the page
    status, body, _ = post_pay(api, tid, bt, shown)
    assert status == 409 and "terms changed" in body
    t = janus.get(tid)
    assert t.payment.order_id is None and t.state.value == "created"
    assert not any(c.kind == "auto_capture_on_verified" for c in t.consents)
    assert any(e["kind"] == "consent_refused" for e in janus.audit.entries(tid))
    assert post_pay(api, tid, bt, shown_terms_hash(api, tid, bt))[0] == 303   # fresh terms are accepted


def test_post_pay_needs_terms_and_the_buyer_token(api, janus):
    tid, bt, st = create(api)
    assert post_pay(api, tid, bt, "")[0] == 400
    assert post_pay(api, tid, st, shown_terms_hash(api, tid, bt))[0] == 404   # the seller cannot consent
    assert post_pay(api, tid, "x" * 43, shown_terms_hash(api, tid, bt))[0] == 404
    assert janus.get(tid).consents == []


def test_match_via_http(api):
    tid, bt, st = create(api)
    status, html, _ = api("GET", f"/t/{tid}?t={bt}")
    assert status == 200 and "Authorize with PayPal" in html
    assert api("POST", f"/api/t/{tid}/release", {}, token=bt)[0] == 409
    pay(api, tid, bt)
    status, t, _ = api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64(with_exif("received_match.jpg"))},
                       token=bt)
    assert status == 200 and t["state"] == "captured" and t["payment"]["captures"]
    status, html, _ = api("GET", f"/t/{tid}?t={bt}")
    assert "Condition met" in html and "PayPal captured 24.00 USD" in html
    img = html.split('class="photo" src="')[1].split('"')[0].replace("&amp;", "&")
    status, _, h = api("GET", img)
    assert status == 200 and h["Content-Type"] == "image/jpeg"
    status, md, _ = api("GET", f"/api/t/{tid}/packet", token=st)
    assert status == 200 and md.startswith("# Decision packet")


def test_mismatch_and_hold_and_seller_refund(api):
    tid, bt, _ = create(api)
    pay(api, tid, bt)
    assert api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64("received_mismatch.jpg")},
               token=bt)[1]["state"] == "voided"

    tid, bt, st = create(api)
    pay(api, tid, bt)
    assert api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64("received_unclear.jpg")},
               token=bt)[1]["state"] == "held"
    html = api("GET", f"/t/{tid}?t={bt}")[1]
    assert "On hold" in html and "Release payment to seller" in html and "Refund the buyer" not in html
    assert api("POST", f"/api/t/{tid}/refund", {}, token=bt)[0] == 404   # the buyer cannot refund
    assert "Refund the buyer" in api("GET", f"/t/{tid}?k={st}")[1]
    status, t, _ = api("POST", f"/api/t/{tid}/refund", {}, token=st)
    assert status == 200 and t["state"] == "voided"
    assert "Refunded by the seller" in api("GET", f"/t/{tid}?t={bt}")[1]


def test_rejects_bad_input(api):
    assert api("POST", "/api/thresholds", {"title": "x", "price": "5"})[0] == 400  # no condition
    assert api("POST", "/api/thresholds", LISTING | {"photo_b64": base64.b64encode(b"not an image").decode()})[0] == 400
    assert api("GET", "/t/th_0000000000000000")[0] == 404
    assert api("GET", "/t/th_0000000000000000/files/../../x")[0] == 404
