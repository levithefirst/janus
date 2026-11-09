"""PayPal Orders v2 / Payments v2 with intent=AUTHORIZE.

    create order -> buyer approves -> authorize -> capture (partial or final) | void
                                                  -> reauthorize (after the honor period)
                                       capture -> refund

`PayPalSandbox` talks to the real sandbox REST API. `PayPalMock` is an offline stand-in with the
same interface and PayPal's error names; it is selected with JANUS_PAYPAL=mock (the default when
no sandbox credentials are set).

Every call that can move money sends a `PayPal-Request-Id` built by `make_request_id(job id, action)`,
so a retry after a timeout or crash replays PayPal's original answer instead of acting twice.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
import zlib
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import requests

from .clock import iso, parse, utcnow
from .constants import AUTH_VALIDITY, REAUTHORIZE_EARLIEST
from .logs import log_event

SANDBOX_API = "https://api-m.sandbox.paypal.com"


def make_request_id(job_id: str, action: str, seq: Optional[int | str] = None) -> str:
    """The idempotency key for one money action on one job, e.g. janus-th_ab12cd34ef-capture-1."""
    rid = f"janus-{job_id}-{action}" + (f"-{seq}" if seq is not None else "")
    if len(rid) > 108:  # PayPal's limit on PayPal-Request-Id
        rid = "janus-" + hashlib.sha256(rid.encode()).hexdigest()
    return rid


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


def _money(amount: Optional[str], currency: Optional[str]) -> Optional[dict]:
    return {"currency_code": currency, "value": amount} if amount is not None else None


def _negative(spec: str) -> dict[str, str]:
    """'capture=INSTRUMENT_DECLINED,void=INTERNAL_SERVER_ERROR' -> {'capture': ..., 'void': ...}"""
    out = {}
    for part in filter(None, (p.strip() for p in spec.split(","))):
        action, _, code = part.partition("=")
        if code:
            out[action.strip()] = code.strip()
    return out


WEBHOOK_HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                   "paypal-transmission-sig", "paypal-transmission-time")


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
            raise PayPalError(f"PayPal OAuth failed: HTTP {r.status_code} "
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
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            err = PayPalError(f"PayPal {action} failed: HTTP {r.status_code}", r.status_code, debug_id, data)
            err.args = (f"PayPal {action} failed: HTTP {r.status_code} {err.issue or ''}".strip(),)
            log_event("paypal", f"{action}_failed", ref, method=method, path=path,
                      status=r.status_code, issue=err.issue, debug_id=debug_id, request_id=request_id)
            raise err
        log_event("paypal", action, ref, method=method, path=path, status=r.status_code,
                  debug_id=debug_id, paypal_status=data.get("status"), request_id=request_id)
        return data

    # --- Orders v2 ---

    def create_order(self, ref: str, amount: str, currency: str, description: str,
                     return_url: str, cancel_url: str, request_id: Optional[str] = None) -> dict:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [{"reference_id": ref, "custom_id": ref, "description": description[:127],
                                "amount": {"currency_code": currency, "value": amount}}],
            "payment_source": {"paypal": {"experience_context": {
                "return_url": return_url, "cancel_url": cancel_url, "user_action": "CONTINUE",
                "shipping_preference": "NO_SHIPPING", "brand_name": "Janus Threshold"}}},
        }
        order = self._call("create_order", "POST", "/v2/checkout/orders", ref, body,
                           request_id=request_id or make_request_id(ref, "create"))
        return {"order_id": order["id"], "status": order.get("status"), "approve_url": _approve_link(order)}

    def get_order(self, ref: str, order_id: str) -> dict:
        return self._call("get_order", "GET", f"/v2/checkout/orders/{order_id}", ref)

    def authorize_order(self, ref: str, order_id: str, request_id: Optional[str] = None) -> dict:
        order = self._call("authorize", "POST", f"/v2/checkout/orders/{order_id}/authorize", ref, {},
                           request_id=request_id or make_request_id(ref, "authorize"))
        auth = auth_from_order(order)
        if not auth:
            raise PayPalError("PayPal authorize returned no authorization", body=order)
        return {"authorization_id": auth["id"], "status": auth.get("status"),
                "expiration_time": auth.get("expiration_time")}

    # --- Payments v2 ---

    def get_authorization(self, ref: str, authorization_id: str) -> dict:
        a = self._call("get_authorization", "GET", f"/v2/payments/authorizations/{authorization_id}", ref)
        return {"status": a.get("status"), "expiration_time": a.get("expiration_time"), "amount": a.get("amount")}

    def capture_authorization(self, ref: str, authorization_id: str, request_id: str,
                              amount: Optional[str] = None, currency: Optional[str] = None,
                              final_capture: bool = True) -> dict:
        body: dict = {"final_capture": final_capture}
        if amount is not None:
            body["amount"] = _money(amount, currency)
        cap = self._call("capture", "POST", f"/v2/payments/authorizations/{authorization_id}/capture",
                         ref, body, request_id=request_id)
        return {"capture_id": cap["id"], "status": cap.get("status"),
                "amount": (cap.get("amount") or {}).get("value", amount)}

    def void_authorization(self, ref: str, authorization_id: str, request_id: str) -> dict:
        res = self._call("void", "POST", f"/v2/payments/authorizations/{authorization_id}/void",
                         ref, None, request_id=request_id)
        return {"status": res.get("status", "VOIDED")}

    def reauthorize(self, ref: str, authorization_id: str, request_id: str,
                    amount: Optional[str] = None, currency: Optional[str] = None) -> dict:
        body = {"amount": _money(amount, currency)} if amount is not None else {}
        a = self._call("reauthorize", "POST", f"/v2/payments/authorizations/{authorization_id}/reauthorize",
                       ref, body, request_id=request_id)
        return {"authorization_id": a["id"], "status": a.get("status"), "expiration_time": a.get("expiration_time")}

    def refund_capture(self, ref: str, capture_id: str, request_id: str,
                       amount: Optional[str] = None, currency: Optional[str] = None) -> dict:
        body = {"amount": _money(amount, currency)} if amount is not None else {}
        r = self._call("refund", "POST", f"/v2/payments/captures/{capture_id}/refund", ref, body,
                       request_id=request_id)
        return {"refund_id": r["id"], "status": r.get("status")}

    # --- Webhooks ---

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes, webhook_id: str) -> bool:
        """Ask PayPal to verify the signature (POST /v1/notifications/verify-webhook-signature)."""
        h = {k.lower(): v for k, v in headers.items()}
        if not webhook_id or any(not h.get(k) for k in WEBHOOK_HEADERS):
            return False
        try:
            event = json.loads(raw_body)
        except ValueError:
            return False
        body = {"auth_algo": h["paypal-auth-algo"], "cert_url": h["paypal-cert-url"],
                "transmission_id": h["paypal-transmission-id"], "transmission_sig": h["paypal-transmission-sig"],
                "transmission_time": h["paypal-transmission-time"], "webhook_id": webhook_id,
                "webhook_event": event}
        try:
            res = self._call("verify_webhook", "POST", "/v1/notifications/verify-webhook-signature", None, body)
        except PayPalError:
            return False
        return res.get("verification_status") == "SUCCESS"


class PayPalMock:
    """Offline simulator persisted to <data_dir>/mock_paypal.json so state survives across CLI runs.

    Models what the hardening depends on: idempotent replay by PayPal-Request-Id, partial and final
    captures, voids, reauthorization (new id, original 29-day expiry kept), refunds, expiry, and
    webhook signatures (HMAC over PayPal's transmission_id|time|webhook_id|crc32(body) message).
    """
    mode = "mock"

    def __init__(self, data_dir: Path, negative_test: str = "", webhook_secret: str = "mock-webhook-secret"):
        from .kv import Backend, FileBackend
        self.kv = data_dir if isinstance(data_dir, Backend) else FileBackend(Path(data_dir))
        self.negative = _negative(negative_test)
        self.webhook_secret = webhook_secret

    def _db(self) -> dict:
        raw = self.kv.get("mock_paypal.json")
        db = json.loads(raw) if raw else {}
        for k in ("orders", "auths", "captures", "requests"):
            db.setdefault(k, {})
        return db

    def _save(self, db: dict) -> None:
        self.kv.put("mock_paypal.json", json.dumps(db, indent=2).encode())

    @staticmethod
    def _fail(code: str, status: int = 422):
        raise PayPalError(f"PayPal (mock) failed: HTTP {status} {code}", status,
                          body={"name": "UNPROCESSABLE_ENTITY" if status == 422 else code,
                                "details": [{"issue": code}]})

    def _inject(self, action: str):
        code = self.negative.get(action)
        if code:
            self._fail(code, 500 if code == "INTERNAL_SERVER_ERROR" else 422)

    def _replay(self, db: dict, rid: Optional[str]) -> Optional[dict]:
        return db["requests"].get(rid) if rid else None

    def _remember(self, db: dict, rid: Optional[str], result: dict) -> dict:
        if rid:
            db["requests"][rid] = result
        self._save(db)
        return result

    # --- Orders v2 ---

    def create_order(self, ref, amount, currency, description, return_url, cancel_url, request_id=None):
        db = self._db()
        rid = request_id or make_request_id(ref, "create")
        if (hit := self._replay(db, rid)) is not None:
            return hit
        order_id = "MOCK-" + secrets.token_hex(6).upper()
        approve_url = f"{return_url}{'&' if '?' in return_url else '?'}token={order_id}"
        db["orders"][order_id] = {"id": order_id, "status": "PAYER_ACTION_REQUIRED", "intent": "AUTHORIZE",
                                  "amount": {"currency_code": currency, "value": amount}, "ref": ref,
                                  "approve_url": approve_url, "auth_id": None}
        log_event("paypal", "create_order", ref, mode="mock", order_id=order_id, amount=amount, currency=currency)
        return self._remember(db, rid, {"order_id": order_id, "status": "PAYER_ACTION_REQUIRED",
                                        "approve_url": approve_url})

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
            out["purchase_units"] = [{"payments": {"authorizations": [db["auths"][order["auth_id"]]]}}]
        return out

    def authorize_order(self, ref, order_id, request_id=None):
        self.get_order(ref, order_id)
        db = self._db()
        rid = request_id or make_request_id(ref, "authorize")
        if (hit := self._replay(db, rid)) is not None:
            return hit
        order = db["orders"][order_id]
        if order["auth_id"]:
            self._fail("ORDER_ALREADY_AUTHORIZED")
        if order["status"] != "APPROVED":
            self._fail("ORDER_NOT_APPROVED")
        auth_id = "MOCKAUTH-" + uuid.uuid4().hex[:10].upper()
        exp = iso(utcnow() + AUTH_VALIDITY)
        order["status"], order["auth_id"] = "COMPLETED", auth_id
        db["auths"][auth_id] = {"id": auth_id, "status": "CREATED", "order_id": order_id, "ref": ref,
                                "amount": order["amount"], "captured": "0.00", "expiration_time": exp,
                                "created_at": iso(), "reauthorized": 0}
        log_event("paypal", "authorize", ref, mode="mock", order_id=order_id, authorization_id=auth_id)
        return self._remember(db, rid, {"authorization_id": auth_id, "status": "CREATED", "expiration_time": exp})

    # --- Payments v2 ---

    def _auth(self, db, authorization_id):
        a = db["auths"].get(authorization_id)
        if not a:
            self._fail("RESOURCE_NOT_FOUND", 404)
        if a["status"] in ("CREATED", "PARTIALLY_CAPTURED") and parse(a["expiration_time"]) < utcnow():
            a["status"] = "EXPIRED"
        return a

    def get_authorization(self, ref, authorization_id):
        db = self._db()
        a = self._auth(db, authorization_id)
        self._save(db)
        log_event("paypal", "get_authorization", ref, mode="mock", paypal_status=a["status"])
        return {"status": a["status"], "expiration_time": a["expiration_time"], "amount": a["amount"]}

    def capture_authorization(self, ref, authorization_id, request_id, amount=None, currency=None,
                              final_capture=True):
        db = self._db()
        if (hit := self._replay(db, request_id)) is not None:  # same key: PayPal replays, never acts twice
            return hit
        self._inject("capture")
        a = self._auth(db, authorization_id)
        if a["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):
            self._fail({"EXPIRED": "AUTHORIZATION_EXPIRED", "VOIDED": "AUTHORIZATION_VOIDED"}.get(
                a["status"], "AUTHORIZATION_ALREADY_CAPTURED"))
        total, captured = Decimal(a["amount"]["value"]), Decimal(a["captured"])
        value = Decimal(amount) if amount is not None else total - captured
        if value <= 0 or captured + value > total:
            self._fail("MAX_CAPTURE_AMOUNT_EXCEEDED")
        cap_id = "MOCKCAP-" + uuid.uuid4().hex[:10].upper()
        a["captured"] = str(captured + value)
        a["status"] = "CAPTURED" if final_capture or captured + value == total else "PARTIALLY_CAPTURED"
        db["captures"][cap_id] = {"id": cap_id, "authorization_id": authorization_id, "amount": str(value),
                                  "refunded": "0.00", "status": "COMPLETED"}
        log_event("paypal", "capture", ref, mode="mock", authorization_id=authorization_id, capture_id=cap_id,
                  amount=str(value), final_capture=final_capture, request_id=request_id)
        return self._remember(db, request_id, {"capture_id": cap_id, "status": "COMPLETED", "amount": str(value)})

    def void_authorization(self, ref, authorization_id, request_id):
        db = self._db()
        if (hit := self._replay(db, request_id)) is not None:
            return hit
        self._inject("void")
        a = self._auth(db, authorization_id)
        if a["status"] == "VOIDED":
            self._fail("AUTHORIZATION_ALREADY_VOIDED")
        if a["status"] not in ("CREATED", "PARTIALLY_CAPTURED"):  # sandbox 2026-11-09: void after partial allowed
            self._fail({"EXPIRED": "AUTHORIZATION_EXPIRED"}.get(a["status"], "AUTHORIZATION_ALREADY_CAPTURED"))
        a["status"] = "VOIDED"
        log_event("paypal", "void", ref, mode="mock", authorization_id=authorization_id, request_id=request_id)
        return self._remember(db, request_id, {"status": "VOIDED"})

    def reauthorize(self, ref, authorization_id, request_id, amount=None, currency=None):
        db = self._db()
        if (hit := self._replay(db, request_id)) is not None:
            return hit
        self._inject("reauthorize")
        a = self._auth(db, authorization_id)
        if a["status"] != "CREATED":
            self._fail("REAUTHORIZATION_NOT_ALLOWED" if a["status"] != "EXPIRED" else "AUTHORIZATION_EXPIRED")
        if utcnow() < parse(a["created_at"]) + REAUTHORIZE_EARLIEST:
            self._fail("REAUTHORIZATION_TOO_SOON")
        new_id = "MOCKAUTH-" + uuid.uuid4().hex[:10].upper()
        a["status"] = "REAUTHORIZED"  # the old id is no longer usable
        db["auths"][new_id] = dict(a, id=new_id, status="CREATED", created_at=iso(),
                                   reauthorized=a["reauthorized"] + 1)  # 29-day expiry is NOT extended
        log_event("paypal", "reauthorize", ref, mode="mock", authorization_id=authorization_id, new_id=new_id)
        return self._remember(db, request_id, {"authorization_id": new_id, "status": "CREATED",
                                               "expiration_time": a["expiration_time"]})

    def refund_capture(self, ref, capture_id, request_id, amount=None, currency=None):
        db = self._db()
        if (hit := self._replay(db, request_id)) is not None:
            return hit
        self._inject("refund")
        c = db["captures"].get(capture_id) or self._fail("RESOURCE_NOT_FOUND", 404)
        left = Decimal(c["amount"]) - Decimal(c["refunded"])
        value = Decimal(amount) if amount is not None else left
        if value <= 0 or value > left:
            self._fail("REFUND_AMOUNT_EXCEEDED")
        c["refunded"] = str(Decimal(c["refunded"]) + value)
        c["status"] = "REFUNDED" if Decimal(c["refunded"]) == Decimal(c["amount"]) else "PARTIALLY_REFUNDED"
        rid = "MOCKREF-" + uuid.uuid4().hex[:10].upper()
        log_event("paypal", "refund", ref, mode="mock", capture_id=capture_id, amount=str(value))
        return self._remember(db, request_id, {"refund_id": rid, "status": "COMPLETED"})

    # --- Webhooks ---

    def _sig(self, transmission_id: str, ts: str, webhook_id: str, raw_body: bytes) -> str:
        msg = f"{transmission_id}|{ts}|{webhook_id}|{zlib.crc32(raw_body)}".encode()
        return base64.b64encode(hmac.new(self.webhook_secret.encode(), msg, hashlib.sha256).digest()).decode()

    def sign_webhook(self, raw_body: bytes, webhook_id: str, transmission_id: Optional[str] = None) -> dict:
        tid, ts = transmission_id or uuid.uuid4().hex, iso()
        return {"paypal-auth-algo": "HMACSHA256-mock", "paypal-cert-url": "mock://cert",
                "paypal-transmission-id": tid, "paypal-transmission-time": ts,
                "paypal-transmission-sig": self._sig(tid, ts, webhook_id, raw_body)}

    def verify_webhook(self, headers: dict[str, str], raw_body: bytes, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        if not webhook_id or any(not h.get(k) for k in WEBHOOK_HEADERS):
            return False
        want = self._sig(h["paypal-transmission-id"], h["paypal-transmission-time"], webhook_id, raw_body)
        return hmac.compare_digest(want, h["paypal-transmission-sig"])

    # --- test helpers ---

    def expire(self, authorization_id: str) -> None:
        db = self._db()
        db["auths"][authorization_id]["expiration_time"] = "2000-01-01T00:00:00+00:00"
        self._save(db)

    def auth(self, authorization_id: str) -> dict:
        return self._db()["auths"][authorization_id]
