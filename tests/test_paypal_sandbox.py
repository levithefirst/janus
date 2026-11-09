"""Checks the real sandbox client's request shapes against a fake HTTP session."""
import json

from eid.paypal import PayPalSandbox


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body
        self.content = json.dumps(body).encode() if body is not None else b""
        self.text = self.content.decode()
        self.headers = {"PayPal-Debug-Id": "dbg123"}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        assert url.endswith("/v1/oauth2/token")
        return Resp(200, {"access_token": "tok", "expires_in": 3600})

    def request(self, method, url, headers=None, data=None, timeout=None):
        body = json.loads(data) if data else None
        self.calls.append((method, url.split(".com", 1)[1], headers, body))
        path = url.split(".com", 1)[1]
        if path == "/v2/checkout/orders":
            return Resp(201, {"id": "ORD1", "status": "PAYER_ACTION_REQUIRED",
                              "links": [{"rel": "payer-action", "href": "https://sandbox.paypal.com/approve?token=ORD1"}]})
        if path.endswith("/authorize"):
            return Resp(201, {"id": "ORD1", "status": "COMPLETED", "purchase_units": [{"payments": {
                "authorizations": [{"id": "AUTH1", "status": "CREATED", "expiration_time": "2026-12-08T00:00:00Z"}]}}]})
        if path.endswith("/capture"):
            return Resp(201, {"id": "CAP1", "status": "COMPLETED"})
        if path.endswith("/void"):
            return Resp(204)
        if method == "GET":
            return Resp(200, {"id": "ORD1", "status": "APPROVED"})
        return Resp(404, {"name": "RESOURCE_NOT_FOUND"})


def test_sandbox_order_authorize_capture_void(tmp_path):
    from eid.logs import setup
    setup(tmp_path)
    s = FakeSession()
    pp = PayPalSandbox("id", "secret", session=s)
    order = pp.create_order("job_1", "180.00", "USD", "Faucet", "http://x/return", "http://x/cancel")
    assert order == {"order_id": "ORD1", "status": "PAYER_ACTION_REQUIRED",
                     "approve_url": "https://sandbox.paypal.com/approve?token=ORD1"}
    method, path, headers, body = s.calls[0]
    assert body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "180.00"}
    assert headers["Authorization"] == "Bearer tok" and headers["PayPal-Request-Id"]
    assert pp.get_order("job_1", "ORD1")["status"] == "APPROVED"
    assert pp.authorize_order("job_1", "ORD1")["authorization_id"] == "AUTH1"
    assert pp.capture_authorization("job_1", "AUTH1") == {"capture_id": "CAP1", "status": "COMPLETED"}
    assert s.calls[-1][1] == "/v2/payments/authorizations/AUTH1/capture"
    assert s.calls[-1][3] == {"final_capture": True}
    assert pp.void_authorization("job_1", "AUTH1") == {"status": "VOIDED"}
    assert s.calls[-1][1] == "/v2/payments/authorizations/AUTH1/void"
