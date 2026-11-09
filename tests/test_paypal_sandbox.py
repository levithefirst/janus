"""The real sandbox client's request shapes and error handling, against a fake HTTP session."""
import json

import pytest
import requests

from janus.paypal import PayPalError, PayPalSandbox


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()
        self.headers = {"PayPal-Debug-Id": "dbg123"}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail or {}

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
        if path.endswith("/capture"):
            return Resp(201, {"id": "CAP1", "status": "COMPLETED"})
        if path.endswith("/void"):
            return Resp(204)
        if path.startswith("/v2/payments/authorizations/"):
            return Resp(200, {"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-12-08T00:00:00Z"})
        return Resp(200, {"id": "ORD1", "status": "APPROVED"})


@pytest.fixture(autouse=True)
def logs(tmp_path):
    from janus.logs import setup
    setup(tmp_path)


def test_order_authorize_capture_void_shapes():
    s = FakeSession()
    pp = PayPalSandbox("id", "secret", session=s)
    order = pp.create_order("th_1", "24.00", "USD", "Mug", "http://x/paypal/return", "http://x/t/th_1")
    assert order["approve_url"].endswith("token=ORD1")
    _, path, headers, body = s.calls[0]
    assert body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "24.00"}
    assert headers["PayPal-Request-Id"] == "janus-create-th_1" and headers["Authorization"] == "Bearer tok"
    assert pp.authorize_order("th_1", "ORD1")["authorization_id"] == "AUTH1"
    assert s.calls[-1][2]["PayPal-Request-Id"] == "janus-authorize-ORD1"
    assert pp.get_authorization("th_1", "AUTH1")["status"] == "CREATED"
    assert s.calls[-1][:2] == ("GET", "/v2/payments/authorizations/AUTH1")
    assert pp.capture_authorization("th_1", "AUTH1") == {"capture_id": "CAP1", "status": "COMPLETED"}
    assert s.calls[-1][1:4:2] == ("/v2/payments/authorizations/AUTH1/capture", {"final_capture": True})
    assert s.calls[-1][2]["PayPal-Request-Id"] == "janus-capture-AUTH1"
    assert pp.void_authorization("th_1", "AUTH1") == {"status": "VOIDED"}
    assert s.calls[-1][2]["PayPal-Request-Id"] == "janus-void-AUTH1"


def test_error_issue_and_retryability():
    s = FakeSession(fail={"/capture": Resp(422, {"name": "UNPROCESSABLE_ENTITY",
                                                 "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}),
                          "/void": Resp(503, {"name": "SERVICE_UNAVAILABLE"})})
    pp = PayPalSandbox("id", "secret", session=s)
    with pytest.raises(PayPalError) as e:
        pp.capture_authorization("th_1", "AUTH1")
    assert e.value.issue == "AUTHORIZATION_EXPIRED" and not e.value.retryable and e.value.debug_id == "dbg123"
    with pytest.raises(PayPalError) as e:
        pp.void_authorization("th_1", "AUTH1")
    assert e.value.retryable


def test_network_error_is_retryable():
    s = FakeSession(fail={"/capture": requests.ConnectionError("reset")})
    with pytest.raises(PayPalError) as e:
        PayPalSandbox("id", "secret", session=s).capture_authorization("th_1", "AUTH1")
    assert e.value.retryable and e.value.status is None


def test_negative_testing_header():
    s = FakeSession()
    pp = PayPalSandbox("id", "secret", session=s, negative_test="capture=INSTRUMENT_DECLINED")
    pp.capture_authorization("th_1", "AUTH1")
    assert json.loads(s.calls[-1][2]["PayPal-Mock-Response"]) == {"mock_application_codes": "INSTRUMENT_DECLINED"}
    pp.void_authorization("th_1", "AUTH1")
    assert "PayPal-Mock-Response" not in s.calls[-1][2]
