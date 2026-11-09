"""The sandbox client's request shapes and error handling (fake HTTP session), and the mock's semantics."""
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from janus import clock
from janus.paypal import PayPalError, PayPalMock, PayPalSandbox, make_request_id


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()
        self.headers = {"PayPal-Debug-Id": "dbg123"}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, fail=None, verify="SUCCESS"):
        self.calls, self.fail, self.verify = [], fail or {}, verify

    def post(self, url, **kw):
        assert url.endswith("/v1/oauth2/token")
        return Resp(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, headers=None, data=None, timeout=None):
        path = url.split(".com", 1)[1]
        self.calls.append((method, path, headers, json.loads(data) if data else None))
        for suffix, resp in self.fail.items():
            if path.endswith(suffix):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        if path == "/v2/checkout/orders":
            return Resp(201, {"id": "ORD1", "status": "PAYER_ACTION_REQUIRED",
                              "links": [{"rel": "payer-action", "href": "https://sandbox.paypal.com/x?token=ORD1"}]})
        if path.endswith("/authorize"):
            return Resp(201, {"id": "ORD1", "status": "COMPLETED", "purchase_units": [{"payments": {
                "authorizations": [{"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-12-08T00:00:00Z"}]}}]})
        if path.endswith("/reauthorize"):
            return Resp(201, {"id": "AUTH2", "status": "CREATED", "expiration_time": "2026-12-08T00:00:00Z"})
        if path.endswith("/capture"):
            return Resp(201, {"id": "CAP1", "status": "COMPLETED", "amount": {"value": "10.00"}})
        if path.endswith("/refund"):
            return Resp(201, {"id": "REF1", "status": "COMPLETED"})
        if path.endswith("/void"):
            return Resp(204)
        if path.endswith("/verify-webhook-signature"):
            return Resp(200, {"verification_status": self.verify})
        if path.startswith("/v2/payments/authorizations/"):
            return Resp(200, {"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-12-08T00:00:00Z"})
        return Resp(200, {"id": "ORD1", "status": "APPROVED"})


@pytest.fixture(autouse=True)
def logs(tmp_path):
    from janus.logs import setup
    setup(tmp_path)


def test_request_ids_are_built_from_job_and_action():
    assert make_request_id("th_ab12cd34ef", "capture", 2) == "janus-th_ab12cd34ef-capture-2"
    assert make_request_id("th_ab12cd34ef", "void") == "janus-th_ab12cd34ef-void"
    assert len(make_request_id("x" * 200, "capture")) <= 108


def test_order_and_payments_shapes():
    s = FakeSession()
    pp = PayPalSandbox("id", "secret", session=s)
    assert pp.create_order("th_1", "24.00", "USD", "Mug", "http://x/r", "http://x/c")["approve_url"].endswith("ORD1")
    _, _, headers, body = s.calls[0]
    assert body["intent"] == "AUTHORIZE" and headers["PayPal-Request-Id"] == "janus-th_1-create"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "24.00"}
    assert pp.authorize_order("th_1", "ORD1")["authorization_id"] == "AUTH1"
    assert s.calls[-1][2]["PayPal-Request-Id"] == "janus-th_1-authorize"

    pp.capture_authorization("th_1", "AUTH1", "janus-th_1-capture-1", amount="10.00", currency="USD",
                             final_capture=False)
    _, path, headers, body = s.calls[-1]
    assert path == "/v2/payments/authorizations/AUTH1/capture" and headers["PayPal-Request-Id"] == "janus-th_1-capture-1"
    assert body == {"final_capture": False, "amount": {"currency_code": "USD", "value": "10.00"}}
    pp.capture_authorization("th_1", "AUTH1", "janus-th_1-capture")
    assert s.calls[-1][3] == {"final_capture": True}

    assert pp.void_authorization("th_1", "AUTH1", "janus-th_1-void") == {"status": "VOIDED"}
    assert s.calls[-1][1:3:1][0] == "/v2/payments/authorizations/AUTH1/void"

    assert pp.reauthorize("th_1", "AUTH1", "janus-th_1-reauthorize-1")["authorization_id"] == "AUTH2"
    assert s.calls[-1][1] == "/v2/payments/authorizations/AUTH1/reauthorize"

    assert pp.refund_capture("th_1", "CAP1", "janus-th_1-refund-CAP1", amount="5.00", currency="USD")["refund_id"] == "REF1"
    assert s.calls[-1][1] == "/v2/payments/captures/CAP1/refund"
    assert s.calls[-1][3] == {"amount": {"currency_code": "USD", "value": "5.00"}}
    assert all(c[2].get("PayPal-Request-Id") for c in s.calls if c[0] == "POST" and "verify" not in c[1])


def test_error_issue_and_retryability():
    s = FakeSession(fail={"/capture": Resp(422, {"name": "UNPROCESSABLE_ENTITY",
                                                 "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}),
                          "/void": Resp(503, {"name": "SERVICE_UNAVAILABLE"})})
    pp = PayPalSandbox("id", "secret", session=s)
    with pytest.raises(PayPalError) as e:
        pp.capture_authorization("th_1", "AUTH1", "r")
    assert e.value.issue == "AUTHORIZATION_EXPIRED" and not e.value.retryable and e.value.debug_id == "dbg123"
    with pytest.raises(PayPalError) as e:
        pp.void_authorization("th_1", "AUTH1", "r")
    assert e.value.retryable


def test_network_error_is_retryable():
    s = FakeSession(fail={"/capture": requests.ConnectionError("reset")})
    with pytest.raises(PayPalError) as e:
        PayPalSandbox("id", "secret", session=s).capture_authorization("th_1", "AUTH1", "r")
    assert e.value.retryable and e.value.status is None


def test_negative_testing_header():
    s = FakeSession()
    pp = PayPalSandbox("id", "secret", session=s, negative_test="capture=INSTRUMENT_DECLINED")
    pp.capture_authorization("th_1", "AUTH1", "r")
    assert json.loads(s.calls[-1][2]["PayPal-Mock-Response"]) == {"mock_application_codes": "INSTRUMENT_DECLINED"}
    pp.void_authorization("th_1", "AUTH1", "r2")
    assert "PayPal-Mock-Response" not in s.calls[-1][2]


def test_webhook_verification_calls_paypal():
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.sandbox.paypal.com/cert",
               "PAYPAL-TRANSMISSION-ID": "t1", "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "now"}
    body = json.dumps({"id": "WH-1", "event_type": "PAYMENT.CAPTURE.COMPLETED"}).encode()
    s = FakeSession()
    assert PayPalSandbox("id", "secret", session=s).verify_webhook(headers, body, "WHID")
    sent = s.calls[-1][3]
    assert sent["webhook_id"] == "WHID" and sent["transmission_id"] == "t1" and sent["webhook_event"]["id"] == "WH-1"
    assert not PayPalSandbox("id", "secret", session=FakeSession(verify="FAILURE")).verify_webhook(headers, body, "WHID")
    assert not PayPalSandbox("id", "secret", session=FakeSession()).verify_webhook({}, body, "WHID")
    assert not PayPalSandbox("id", "secret", session=FakeSession()).verify_webhook(headers, body, "")


# ---------- mock semantics the service relies on ----------

@pytest.fixture
def mock_auth(tmp_path):
    pp = PayPalMock(tmp_path)
    o = pp.create_order("th_1", "30.00", "USD", "x", "http://r", "http://c")
    a = pp.authorize_order("th_1", o["order_id"])
    return pp, a["authorization_id"]


def test_mock_replays_same_request_id(mock_auth):
    pp, auth = mock_auth
    first = pp.capture_authorization("th_1", auth, "janus-th_1-capture")
    assert pp.capture_authorization("th_1", auth, "janus-th_1-capture") == first  # replay, not a second capture
    with pytest.raises(PayPalError) as e:
        pp.capture_authorization("th_1", auth, "a-different-key")
    assert e.value.issue == "AUTHORIZATION_ALREADY_CAPTURED"


def test_mock_partial_then_final_capture_and_refund(mock_auth):
    pp, auth = mock_auth
    c1 = pp.capture_authorization("th_1", auth, "k1", amount="10.00", currency="USD", final_capture=False)
    assert pp.auth(auth)["status"] == "PARTIALLY_CAPTURED"
    with pytest.raises(PayPalError):
        pp.capture_authorization("th_1", auth, "k2", amount="25.00", currency="USD", final_capture=False)
    pp.capture_authorization("th_1", auth, "k3", amount="5.00", currency="USD", final_capture=True)
    assert pp.auth(auth)["status"] == "CAPTURED" and pp.auth(auth)["captured"] == "15.00"
    assert pp.refund_capture("th_1", c1["capture_id"], "r1", amount="4.00", currency="USD")["status"] == "COMPLETED"
    with pytest.raises(PayPalError):
        pp.refund_capture("th_1", c1["capture_id"], "r2", amount="7.00", currency="USD")


def test_mock_reauthorize_keeps_original_expiry(tmp_path):
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    with clock.frozen(start) as t:
        pp = PayPalMock(tmp_path)
        o = pp.create_order("th_1", "30.00", "USD", "x", "http://r", "http://c")
        a = pp.authorize_order("th_1", o["order_id"])
        with pytest.raises(PayPalError) as e:
            pp.reauthorize("th_1", a["authorization_id"], "re1")
        assert e.value.issue == "REAUTHORIZATION_TOO_SOON"
        t.advance(timedelta(days=4))
        r = pp.reauthorize("th_1", a["authorization_id"], "re2")
        assert r["authorization_id"] != a["authorization_id"] and r["expiration_time"] == a["expiration_time"]
        with pytest.raises(PayPalError):
            pp.capture_authorization("th_1", a["authorization_id"], "c")  # old id is dead


def test_mock_webhook_signature(tmp_path):
    pp = PayPalMock(tmp_path)
    body = b'{"id":"WH-1"}'
    h = pp.sign_webhook(body, "WHID")
    assert pp.verify_webhook(h, body, "WHID")
    assert not pp.verify_webhook(h, body + b" ", "WHID")       # body tampered
    assert not pp.verify_webhook(h, body, "OTHER")             # wrong webhook id
    assert not pp.verify_webhook({**h, "paypal-transmission-sig": "x"}, body, "WHID")
