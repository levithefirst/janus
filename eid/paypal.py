"""PayPal Orders v2 with intent=AUTHORIZE: create order -> authorize -> capture or void.

`PayPalSandbox` talks to the real sandbox REST API. `PayPalMock` is an offline
stand-in with the same interface (it simulates the buyer approving the order),
so the full loop can be tested without credentials or network access.
"""
from __future__ import annotations

import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import requests

from .logs import log_event

SANDBOX_API = "https://api-m.sandbox.paypal.com"


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None, body: Any = None):
        super().__init__(message)
        self.status, self.debug_id, self.body = status, debug_id, body


def _approve_link(order: dict) -> Optional[str]:
    for link in order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link["href"]
    return None


class PayPalSandbox:
    mode = "sandbox"

    def __init__(self, client_id: str, client_secret: str, base_url: str = SANDBOX_API,
                 session: requests.Session | None = None):
        if not client_id or not client_secret:
            raise ValueError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are required for sandbox mode")
        self.client_id, self.client_secret, self.base_url = client_id, client_secret, base_url
        self.http = session or requests.Session()
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    # --- plumbing ---

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        r = self.http.post(f"{self.base_url}/v1/oauth2/token", auth=(self.client_id, self.client_secret),
                           data={"grant_type": "client_credentials"}, timeout=30)
        if r.status_code != 200:
            log_event("paypal", "oauth_token_failed", status=r.status_code)
            raise PayPalError(f"PayPal OAuth failed: HTTP {r.status_code} {r.text[:300]}", r.status_code)
        body = r.json()
        self._token, self._token_expiry = body["access_token"], time.time() + int(body.get("expires_in", 300))
        log_event("paypal", "oauth_token_ok", expires_in=body.get("expires_in"))
        return self._token

    def _call(self, action: str, method: str, path: str, job_id: str | None, body: dict | None = None,
              request_id: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json",
                   "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id  # idempotency: safe to retry
        r = self.http.request(method, f"{self.base_url}{path}", headers=headers,
                              data=json.dumps(body) if body is not None else None, timeout=30)
        debug_id = r.headers.get("PayPal-Debug-Id")
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text}
        if r.status_code >= 400:
            log_event("paypal", f"{action}_failed", job_id, method=method, path=path,
                      status=r.status_code, debug_id=debug_id, error=data)
            detail = data.get("details") or data.get("message") or data
            raise PayPalError(f"PayPal {action} failed: HTTP {r.status_code}: {detail}",
                              r.status_code, debug_id, data)
        log_event("paypal", action, job_id, method=method, path=path, status=r.status_code,
                  debug_id=debug_id, paypal_status=data.get("status"))
        return data

    # --- Orders v2 ---

    def create_order(self, job_id: str, amount: str, currency: str, description: str,
                     return_url: str, cancel_url: str) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{
                "reference_id": job_id,
                "custom_id": job_id,
                "description": description[:127],
                "amount": {"currency_code": currency, "value": amount},
            }],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING",
                "brand_name": "Eid",
            }}},
        }
        order = self._call("create_order", "POST", "/v2/checkout/orders", job_id, body,
                           request_id=f"eid-create-{job_id}")
        return {"order_id": order["id"], "status": order.get("status"), "approve_url": _approve_link(order)}

    def get_order(self, job_id: str, order_id: str) -> dict:
        return self._call("get_order", "GET", f"/v2/checkout/orders/{order_id}", job_id)

    def authorize_order(self, job_id: str, order_id: str) -> dict:
        order = self._call("authorize_order", "POST", f"/v2/checkout/orders/{order_id}/authorize", job_id, {},
                           request_id=f"eid-authorize-{order_id}")
        auth = order["purchase_units"][0]["payments"]["authorizations"][0]
        log_event("paypal", "authorization_created", job_id, authorization_id=auth["id"],
                  authorization_status=auth.get("status"), expiration_time=auth.get("expiration_time"))
        return {"authorization_id": auth["id"], "status": auth.get("status"),
                "expiration_time": auth.get("expiration_time")}

    def capture_authorization(self, job_id: str, authorization_id: str) -> dict:
        cap = self._call("capture_authorization", "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                         job_id, {"final_capture": True}, request_id=f"eid-capture-{authorization_id}")
        return {"capture_id": cap["id"], "status": cap.get("status")}

    def void_authorization(self, job_id: str, authorization_id: str) -> dict:
        res = self._call("void_authorization", "POST", f"/v2/payments/authorizations/{authorization_id}/void",
                         job_id, None, request_id=f"eid-void-{authorization_id}")
        return {"status": res.get("status", "VOIDED")}


class PayPalMock:
    """Offline simulator persisted to <data_dir>/mock_paypal.json so state survives across CLI runs."""
    mode = "mock"

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "mock_paypal.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _db(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.is_file() else {"orders": {}, "auths": {}}

    def _save(self, db: dict) -> None:
        self.path.write_text(json.dumps(db, indent=2))

    def create_order(self, job_id, amount, currency, description, return_url, cancel_url):
        db = self._db()
        order_id = "MOCK-" + secrets.token_hex(6).upper()
        db["orders"][order_id] = {"id": order_id, "status": "PAYER_ACTION_REQUIRED", "intent": "AUTHORIZE",
                                  "amount": {"currency_code": currency, "value": amount}, "job_id": job_id}
        self._save(db)
        approve_url = f"{return_url}{'&' if '?' in return_url else '?'}token={order_id}"
        log_event("paypal", "create_order", job_id, mode="mock", order_id=order_id, amount=amount, currency=currency)
        return {"order_id": order_id, "status": "PAYER_ACTION_REQUIRED", "approve_url": approve_url}

    def get_order(self, job_id, order_id):
        db = self._db()
        order = db["orders"].get(order_id)
        if not order:
            raise PayPalError(f"mock order {order_id} not found", 404)
        if order["status"] == "PAYER_ACTION_REQUIRED":
            order["status"] = "APPROVED"  # simulate the buyer approving on PayPal
            self._save(db)
            log_event("paypal", "mock_buyer_approved", job_id, mode="mock", order_id=order_id)
        log_event("paypal", "get_order", job_id, mode="mock", order_id=order_id, paypal_status=order["status"])
        return order

    def authorize_order(self, job_id, order_id):
        self.get_order(job_id, order_id)
        db = self._db()
        order = db["orders"][order_id]
        if order["status"] != "APPROVED":
            raise PayPalError(f"ORDER_NOT_APPROVED: order is {order['status']}", 422)
        auth_id = "MOCKAUTH-" + uuid.uuid4().hex[:10].upper()
        order["status"] = "COMPLETED"
        db["auths"][auth_id] = {"id": auth_id, "status": "CREATED", "order_id": order_id, "amount": order["amount"]}
        self._save(db)
        log_event("paypal", "authorize_order", job_id, mode="mock", order_id=order_id, authorization_id=auth_id,
                  authorization_status="CREATED")
        return {"authorization_id": auth_id, "status": "CREATED", "expiration_time": None}

    def _auth(self, db, authorization_id):
        auth = db["auths"].get(authorization_id)
        if not auth:
            raise PayPalError(f"mock authorization {authorization_id} not found", 404)
        if auth["status"] != "CREATED":
            raise PayPalError(f"AUTHORIZATION_ALREADY_{auth['status']}", 422)
        return auth

    def capture_authorization(self, job_id, authorization_id):
        db = self._db()
        auth = self._auth(db, authorization_id)
        auth["status"] = "CAPTURED"
        capture_id = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
        self._save(db)
        log_event("paypal", "capture_authorization", job_id, mode="mock", authorization_id=authorization_id,
                  capture_id=capture_id, paypal_status="COMPLETED")
        return {"capture_id": capture_id, "status": "COMPLETED"}

    def void_authorization(self, job_id, authorization_id):
        db = self._db()
        auth = self._auth(db, authorization_id)
        auth["status"] = "VOIDED"
        self._save(db)
        log_event("paypal", "void_authorization", job_id, mode="mock", authorization_id=authorization_id,
                  paypal_status="VOIDED")
        return {"status": "VOIDED"}
