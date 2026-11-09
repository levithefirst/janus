"""Drive the loop through HTTP: create, buyer link -> PayPal -> return URL, upload, outcome page."""
import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from conftest import LISTING, PHOTOS
from janus.server import make_handler


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


@pytest.fixture
def api(janus):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(janus))
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
        raw = r.read()
        try:
            return r.status, json.loads(raw), r.headers
        except (json.JSONDecodeError, UnicodeDecodeError):
            return r.status, raw.decode(errors="replace"), r.headers
    yield call
    httpd.shutdown()


def b64(name):
    return "data:image/jpeg;base64," + base64.b64encode((PHOTOS / name).read_bytes()).decode()


def create(api):
    status, d, _ = api("POST", "/api/thresholds", LISTING | {"photo_b64": b64("listing_red_mug.jpg")})
    assert status == 201 and "seller_key" not in d["threshold"]
    return d


def pay(api, tid):
    status, _, h = api("GET", f"/t/{tid}/pay")  # buyer taps "Authorize with PayPal"
    assert status == 303
    order = h["Location"].split("token=")[1]
    status, _, h = api("GET", f"/paypal/return?token={order}")  # PayPal sends the buyer back
    assert status == 303 and h["Location"] == f"/t/{tid}"


def test_match_via_http(api):
    d = create(api)
    tid = d["id"]
    assert d["seller_link"].endswith("?k=" + d["seller_link"].split("?k=")[1])
    status, html, _ = api("GET", f"/t/{tid}")
    assert status == 200 and "Authorize with PayPal" in html
    assert api("POST", f"/api/t/{tid}/release")[0] == 409
    pay(api, tid)
    status, t, _ = api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64("received_match.jpg"), "note": "hi"})
    assert status == 200 and t["state"] == "captured" and t["payment"]["capture_id"]
    status, html, _ = api("GET", f"/t/{tid}")
    assert "Condition met" in html and "PayPal captured 24.00 USD" in html
    status, img, h = api("GET", f"/t/{tid}/files/evidence/V1.jpg")
    assert status == 200 and h["Content-Type"] == "image/jpeg"


def test_mismatch_and_hold_and_seller_refund(api):
    tid = create(api)["id"]
    pay(api, tid)
    assert api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64("received_mismatch.jpg")})[1]["state"] == "voided"

    d = create(api)
    tid, key = d["id"], d["seller_link"].split("?k=")[1]
    pay(api, tid)
    assert api("POST", f"/api/t/{tid}/evidence", {"photo_b64": b64("received_unclear.jpg")})[1]["state"] == "held"
    html = api("GET", f"/t/{tid}")[1]
    assert "On hold" in html and "Release payment to seller" in html and "Refund the buyer" not in html
    assert api("POST", f"/api/t/{tid}/refund", {"key": "wrong"})[0] == 403
    assert "Refund the buyer" in api("GET", f"/t/{tid}?k={key}")[1]
    status, t, _ = api("POST", f"/api/t/{tid}/refund", {"key": key})
    assert status == 200 and t["state"] == "voided"
    assert "Refunded by the seller" in api("GET", f"/t/{tid}")[1]


def test_rejects_bad_input(api):
    assert api("POST", "/api/thresholds", {"title": "x", "price": "5"})[0] == 400  # no condition
    assert api("POST", "/api/thresholds", LISTING | {"photo_b64": base64.b64encode(b"not an image").decode()})[0] == 400
    assert api("GET", "/t/th_0000000000")[0] == 404
    assert api("GET", "/t/th_0000000000/files/../../x")[0] == 404
