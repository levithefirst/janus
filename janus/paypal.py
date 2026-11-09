"""PayPal Orders v2 with intent=AUTHORIZE: create order -> authorize -> capture or void.

`PayPalSandbox` talks to the real sandbox REST API. `PayPalMock` is an offline stand-in with
the same interface and the same error names, so the loop can be tested without credentials.

Every call that changes money sends a deterministic `PayPal-Request-Id`, so a retry after a
timeout or crash replays the original result instead of acting twice.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from .logs import log_event

SANDBOX_API = "https://api-m.sandbox.paypal.com"
HONOR_PERIOD = timedelta(days=3)       # PayPal guarantees funds for 3 days
AUTH_VALIDITY = timedelta(days=29)     # an authorization can be captured for up to 29 days


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None, body: Any = None):
        super().__init__(message)
        self.status, self.debug_id, self.body = status, debug_id, body

    @property
    def issue(self) -> str:
        """PayPal's machine-readable error, e.g. AUTHORIZATION_EXPIRED. Empty if unknown."""
        b = self.body if isinstance(self.body, dict) else {}
        details = b.get("details") or []
        if details and isinstance(details[0], dict) and details[0].get("issue"):
            return details[0]["issue"]
        return b.get("name", "") or ""

    @property
    def retryable(self) -> bool:
        """Network errors, timeouts, 429 and 5xx: safe to retry with the same PayPal-Request-Id."""
        return self.status is None or self.status == 429 or self.status >= 500


def _approve_link(order: dict) -> Optional[str]:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


def auth_from_order(order: dict) -> Optional[dict]:
    try:
        return order["purchase_units"][0]["payments"]["authorizations"][0]
    except (KeyError, IndexError, TypeError):
        return None


def _negative(spec: str) -> dict[str, str]:
    """'capture=INSTRUMENT_DECLINED,void=INTERNAL_SERVER_ERROR' -> {'capture': ..., 'void': ...}"""
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        action, _, code = part.partition("=")
        if code:
            out[action.strip()] = code.strip()
    return out


class PayPalSandbox:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_API,
                 session: requests.Session | None = None, negative_test: str = ""):
        if not client_id or not client_secret:
            raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required for sandbox mode")
        self.client_id, self.client_secret, self.base_url = client_id, client_secret, base_url
        self.http = session or requests.Session()
        self.negative = _negative(negative_test)
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    # --- plumbing ---

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        try:
            r = self.http.post(f"{self.base_url}/v1/oauth2/token", auth=(self.client_id, self.client_secret),
                               data={"grant_type": "client_credentials"}, timeout=30)
        except requests.RequestException as e:
            raise PayPalError(f"PayPal OAuth: network error: {e}") from e
        if r.status_code != 200:
            log_event("paypal", "oauth_token_failed", status=r.status_code)
            raise PayPalError(f"PayPal OAuth failed: HTTP {r.status_code} {r.text[:300]} "
                              "(check PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are SANDBOX credentials)",
                              r.status_code)
        body = r.json()
        self._token, self._token_expiry = body["access_token"], time.time() + int(body.get("expires_in", 300))
        log_event("paypal", "oauth_token_ok", expires_in=body.get("expires_in"))
        return self._token

    def _call(self, action: str, method: str, path: str, ref: str | None, body: dict | None = None,
              request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if action in self.negative:  # sandbox negative testing
            headers["PayPal-Mock-Response"] = json.dumps({"mock_application_codes": self.negative[action]})
        try:
            r = self.http.request(method, f"{self.base_url}{path}", headers=headers,
                                  data=json.dumps(body) if body is not None else None, timeout=30)
        except requests.RequestException as e:
            log_event("paypal", f"{action}_failed", ref, method=method, path=path, error=f"network: {e}")
            raise PayPalError(f"PayPal {action}: network error: {e}") from e
        debug_id = r.headers.get("PayPal-Debug-Id")
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            err = PayPalError(f"PayPal {action} failed: HTTP {r.status_code}", r.status_code, debug_id, data)
            err.args = (f"PayPal {action} failed: HTTP {r.status_code} {err.issue or data}",)
            log_event("paypal", f"{action}_failed", ref, method=method, path=path,
                      status=r.status_code, issue=err.issue, debug_id=debug_id)
            raise err
        log_event("paypal", action, ref, method=method, path=path, status=r.status_code,
                  debug_id=debug_id, paypal_status=data.get("status"))
        return data

    # --- Orders v2 ---

    def create_order(self, ref: str, amount: str, currency: str, description: str,
                     return_url: str, cancel_url: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": ref,
                "custom_id": ref,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": amount},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING",
                "brand_name": "Janus Threshold",
            }}},
        }
        order = self._call("create_order", "POST", "/v2/checkout/orders", ref, body,
                           request_id=f"janus-create-{ref}")
        return {"order_id": order["id"], "status": order.get("status"), "approve_url": _approve_link(order)}

    def get_order(self, ref: str, order_id: str) -> dict:
        return self._call("get_order", "GET", f"/v2/checkout/orders/{order_id}", ref)

    def authorize_order(self, ref: str, order_id: str) -> dict:
        order = self._call("authorize", "POST", f"/v2/checkout/orders/{order_id}/authorize", ref, {},
                           request_id=f"janus-authorize-{order_id}")
        auth = auth_from_order(order)
        if not auth:
            raise PayPalError("PayPal authorize returned no authorization", body=order)
        log_event("paypal", "authorization_created", ref, authorization_id=auth["id"],
                  authorization_status=auth.get("status"), expiration_time=auth.get("expiration_time"))
        return {"authorization_id": auth["id"], "status": auth.get("status"),
                "expiration_time": auth.get("expiration_time")}

    def get_authorization(self, ref: str, authorization_id: str) -> dict:
        a = self._call("get_authorization", "GET", f"/v2/payments/authorizations/{authorization_id}", ref)
        return {"status": a.get("status"), "expiration_time": a.get("expiration_time"),
                "amount": a.get("amount")}

    def capture_authorization(self, ref: str, authorization_id: str) -> dict:
        cap = self._call("capture", "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                         ref, {"final_capture": True}, request_id=f"janus-capture-{authorization_id}")
        return {"capture_id": cap["id"], "status": cap.get("status")}

    def void_authorization(self, ref: str, authorization_id: str) -> dict:
        res = self._call("void", "POST", f"/v2/payments/authorizations/{authorization_id}/void",
                         ref, None, request_id=f"janus-void-{authorization_id}")
        return {"status": res.get("status", "VOIDED")}


class PayPalMock:
    """Offline simulator persisted to <data_dir>/mock_paypal.json so state survives across CLI runs.

    It simulates the buyer approving the order, uses PayPal's real error names, honours the same
    negative-test spec as the sandbox client, and `expire()` lets tests age an authorization.
    """
    mode = "mock"

    def __init__(self, data_dir: Path, negative_test: str = ""):
        self.path = Path(data_dir) / "mock_paypal.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.negative = _negative(negative_test)

    def _db(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.is_file() else {"orders": {}, "auths": {}}

    def _save(self, db: dict) -> None:
        self.path.write_text(json.dumps(db, indent=2))

    @staticmethod
    def _fail(code: str, status: int = 422):
        raise PayPalError(f"PayPal (mock) failed: HTTP {status} {code}", status,
                          body={"name": "UNPROCESSABLE_ENTITY" if status == 422 else code,
                                "details": [{"issue": code}]})

    def _inject(self, action: str):
        code = self.negative.get(action)
        if code:
            self._fail(code, 500 if code == "INTERNAL_SERVER_ERROR" else 422)

    def create_order(self, ref, amount, currency, description, return_url, cancel_url):
        db = self._db()
        for o in db["orders"].values():  # idempotent, like PayPal-Request-Id
            if o["ref"] == ref:
                return {"order_id": o["id"], "status": o["status"], "approve_url": o["approve_url"]}
        order_id = "MOCK-" + secrets.token_hex(6).upper()
        approve_url = f"{return_url}{'&' if '?' in return_url else '?'}token={order_id}"
        db["orders"][order_id] = {"id": order_id, "status": "PAYER_ACTION_REQUIRED", "intent": "AUTHORIZE",
                                  "amount": {"currency_code": currency, "value": amount}, "ref": ref,
                                  "approve_url": approve_url, "auth_id": None}
        self._save(db)
        log_event("paypal", "create_order", ref, mode="mock", order_id=order_id, amount=amount, currency=currency)
        return {"order_id": order_id, "status": "PAYER_ACTION_REQUIRED", "approve_url": approve_url}

    def get_order(self, ref, order_id):
        db = self._db()
        order = db["orders"].get(order_id)
        if not order:
            self._fail("RESOURCE_NOT_FOUND", 404)
        if order["status"] == "PAYER_ACTION_REQUIRED":
            order["status"] = "APPROVED"  # simulate the buyer approving on PayPal
            self._save(db)
            log_event("paypal", "mock_buyer_approved", ref, mode="mock", order_id=order_id)
        out = dict(order)
        if order["auth_id"]:
            a = db["auths"][order["auth_id"]]
            out["purchase_units"] = [{"payments": {"authorizations": [a]}}]
        return out

    def authorize_order(self, ref, order_id):
        self.get_order(ref, order_id)
        db = self._db()
        order = db["orders"][order_id]
        if order["auth_id"]:  # same PayPal-Request-Id replays the original answer
            a = db["auths"][order["auth_id"]]
            return {"authorization_id": a["id"], "status": a["status"], "expiration_time": a["expiration_time"]}
        if order["status"] != "APPROVED":
            self._fail("ORDER_NOT_APPROVED")
        auth_id = "MOCKAUTH-" + uuid.uuid4().hex[:10].upper()
        exp = (datetime.now(timezone.utc) + AUTH_VALIDITY).isoformat(timespec="seconds")
        order["status"], order["auth_id"] = "COMPLETED", auth_id
        db["auths"][auth_id] = {"id": auth_id, "status": "CREATED", "order_id": order_id,
                                "amount": order["amount"], "expiration_time": exp, "capture_id": None}
        self._save(db)
        log_event("paypal", "authorize", ref, mode="mock", order_id=order_id, authorization_id=auth_id)
        return {"authorization_id": auth_id, "status": "CREATED", "expiration_time": exp}

    def _auth(self, db, authorization_id):
        a = db["auths"].get(authorization_id)
        if not a:
            self._fail("RESOURCE_NOT_FOUND", 404)
        if a["status"] == "CREATED" and a["expiration_time"] < datetime.now(timezone.utc).isoformat():
            a["status"] = "EXPIRED"
        return a

    def get_authorization(self, ref, authorization_id):
        db = self._db()
        a = self._auth(db, authorization_id)
        self._save(db)
        log_event("paypal", "get_authorization", ref, mode="mock", paypal_status=a["status"])
        return {"status": a["status"], "expiration_time": a["expiration_time"], "amount": a["amount"]}

    def capture_authorization(self, ref, authorization_id):
        self._inject("capture")
        db = self._db()
        a = self._auth(db, authorization_id)
        if a["status"] == "CAPTURED" and a["capture_id"]:  # idempotent replay
            return {"capture_id": a["capture_id"], "status": "COMPLETED"}
        if a["status"] != "CREATED":
            self._fail({"EXPIRED": "AUTHORIZATION_EXPIRED", "VOIDED": "AUTHORIZATION_VOIDED"}.get(
                a["status"], "AUTHORIZATION_ALREADY_CAPTURED"))
        a["status"], a["capture_id"] = "CAPTURED", "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
        self._save(db)
        log_event("paypal", "capture", ref, mode="mock", authorization_id=authorization_id,
                  capture_id=a["capture_id"], paypal_status="COMPLETED")
        return {"capture_id": a["capture_id"], "status": "COMPLETED"}

    def void_authorization(self, ref, authorization_id):
        self._inject("void")
        db = self._db()
        a = self._auth(db, authorization_id)
        if a["status"] == "VOIDED":
            return {"status": "VOIDED"}
        if a["status"] != "CREATED":
            self._fail({"EXPIRED": "AUTHORIZATION_EXPIRED"}.get(a["status"], "AUTHORIZATION_ALREADY_CAPTURED"))
        a["status"] = "VOIDED"
        self._save(db)
        log_event("paypal", "void", ref, mode="mock", authorization_id=authorization_id, paypal_status="VOIDED")
        return {"status": "VOIDED"}

    # --- test helper ---

    def expire(self, authorization_id: str) -> None:
        db = self._db()
        db["auths"][authorization_id]["expiration_time"] = "2000-01-01T00:00:00+00:00"
        self._save(db)
