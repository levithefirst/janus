"""Orchestration: each public method is one step of the loop and one guarded state change.

    create -> create_payment -> authorize -> submit_evidence -> verify -> (capture | void | hold)

`verify` is where the AI changes the payment: the verdict goes through `policy.decide`, and
capture or void is called on PayPal right away. A human only acts on a HOLD: the buyer can
release the money (capture) and the seller can refund (void). Neither can take money from
the other.
"""
from __future__ import annotations

import hashlib
import io
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Optional, Union

from .ai import ClaudeAI, MockAI
from .config import Config
from .logs import log_event, now, setup
from .models import EvidenceItem, Listing
from .clock import iso, parse, utcnow
from .constants import AUTH_VALIDITY, HONOR_PERIOD
from .paypal import PayPalError, make_request_id, PayPalMock, PayPalSandbox, auth_from_order
from .policy import decide
from .state import HOLDS_FUNDS, TERMINAL, State, TransitionError, check
from .store import Outcome, Store, Threshold, VerdictRecord

ImageInput = Union[str, Path, bytes]
MAX_IMAGE_BYTES = 12 * 1024 * 1024
_MAGIC = {b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG": ("image/png", ".png"),
          b"GIF8": ("image/gif", ".gif"), b"RIFF": ("image/webp", ".webp")}


class JanusError(Exception):
    pass


def _money(amount: str) -> str:
    try:
        d = Decimal(str(amount).replace("$", "").strip()).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise JanusError(f"invalid price {amount!r}")
    if d <= 0:
        raise JanusError("price must be positive")
    return str(d)


def _parse_ts(s: Optional[str]):
    return parse(s)


def normalize_image(data: bytes) -> tuple[bytes, str, str]:
    """Return (bytes, media_type, ext). With Pillow: upright, max 1600 px, JPEG, metadata stripped."""
    if not data:
        raise JanusError("empty image")
    if len(data) > MAX_IMAGE_BYTES:
        raise JanusError("image is larger than 12 MB")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        for magic, (mt, ext) in _MAGIC.items():
            if data.startswith(magic):
                return data, mt, ext
        raise JanusError("not a JPEG, PNG, GIF or WebP image")
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except Exception:
        raise JanusError("could not read that file as an image")
    img.thumbnail((1600, 1600))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue(), "image/jpeg", ".jpg"


class Janus:
    def __init__(self, config: Config, ai=None, paypal=None):
        self.config = config
        setup(config.data_dir)
        self.store = Store(config.data_dir)
        self.ai = ai or (ClaudeAI(config.model) if config.ai_mode == "claude" else MockAI())
        self.paypal = paypal or (
            PayPalSandbox(config.paypal_client_id, config.paypal_client_secret,
                          negative_test=config.paypal_negative_test)
            if config.paypal_mode == "sandbox" else PayPalMock(config.data_dir, config.paypal_negative_test))

    @classmethod
    def from_env(cls) -> "Janus":
        return cls(Config.from_env())

    # --- helpers ---

    @contextmanager
    def _locked(self, tid: str) -> Iterator[Threshold]:
        """Load-modify-save under the threshold's lock; saves are compare-and-set on top."""
        with self.store.lock(tid):
            yield self.store.load(tid)

    def _move(self, t: Threshold, target: State, **data) -> None:
        check(t.state, target)
        t.events.append(log_event("state", "transition", t.id, from_state=t.state.value,
                                  to_state=target.value, **data))
        t.state = target
        self.store.save(t)

    def _record(self, t: Threshold, category: str, action: str, **data) -> None:
        t.events.append(log_event(category, action, t.id, **data))

    def _ingest(self, t: Threshold, image: ImageInput, subdir: str, stem: str) -> tuple[str, str, str]:
        raw = Path(image).read_bytes() if isinstance(image, (str, Path)) else image
        data, media_type, ext = normalize_image(raw)
        d = self.store.dir(t.id) / subdir
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{stem}{ext}").write_bytes(data)
        return f"{subdir}/{stem}{ext}", media_type, hashlib.sha256(data).hexdigest()

    def get(self, tid: str) -> Threshold:
        return self.store.load(tid)

    def buyer_link(self, t: Threshold) -> str:
        return f"{self.config.base_url}/t/{t.id}"

    def seller_link(self, t: Threshold) -> str:
        return f"{self.config.base_url}/t/{t.id}?k={t.seller_key}"

    def is_seller(self, t: Threshold, key: Optional[str]) -> bool:
        import hmac
        return bool(key) and hmac.compare_digest(key, t.seller_key)

    @staticmethod
    def honor_period_over(t: Threshold) -> bool:
        at = _parse_ts(t.payment.authorized_at)
        return bool(at) and utcnow() > at + HONOR_PERIOD

    # --- 1 + 2: seller states the condition, AI compiles it ---

    def create(self, title: str, price: str, condition: str, description: str = "",
               currency: str = "USD", photos: Optional[list[ImageInput]] = None) -> Threshold:
        title, condition = title.strip(), condition.strip()
        if not title:
            raise JanusError("the listing needs a title")
        if not condition:
            raise JanusError("state the condition the payment depends on")
        listing = Listing(title=title, description=description.strip(), price=_money(price),
                          currency=(currency or "USD").upper())
        tid = self.store.new_id()
        draft = Threshold.model_construct(id=tid)
        listing.photos = [self._ingest(draft, p, "listing", f"L{i}")[0] for i, p in enumerate(photos or [], 1)]
        compiled = self.ai.compile_condition(condition, listing, self.store.dir(tid), ref=tid)
        t = Threshold(id=tid, seller_key=self.store.new_key(), state=State.CREATED, created_at=now(),
                      updated_at=now(), listing=listing, condition=condition, compiled=compiled)
        self._record(t, "ai", "condition_compiled", requirements=[f"{r.id}{'*' if r.critical else ''}"
                                                                  for r in compiled.requirements])
        self._record(t, "state", "created", state=State.CREATED.value)
        self.store.save(t)
        return t

    # --- 3: buyer authorizes on PayPal (money held, not taken) ---

    def create_payment(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.state != State.CREATED:  # idempotent: re-issue the same order
                if not t.payment.order_id:
                    check(t.state, State.PAYMENT_PENDING)
                return t
            base = self.config.base_url
            order = self.paypal.create_order(t.id, t.amount, t.currency, t.listing.title,
                                             return_url=f"{base}/paypal/return", cancel_url=f"{base}/t/{t.id}")
            t.payment.order_id, t.payment.approve_url = order["order_id"], order["approve_url"]
            self._record(t, "paypal", "order_created", order_id=order["order_id"])
            self._move(t, State.PAYMENT_PENDING, buyer_link=self.buyer_link(t))
            return t

    def authorize(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.state in HOLDS_FUNDS or t.state in TERMINAL:  # return URL hit twice, etc.
                return t
            check(t.state, State.AUTHORIZED)
            order = self.paypal.get_order(t.id, t.payment.order_id)
            existing = auth_from_order(order)
            if existing:  # already authorized on PayPal (we crashed before saving): adopt it
                auth = {"authorization_id": existing["id"], "status": existing.get("status"),
                        "expiration_time": existing.get("expiration_time")}
                self._record(t, "paypal", "authorization_adopted", authorization_id=existing["id"])
            elif order.get("status") != "APPROVED":
                raise JanusError(f"the buyer has not approved the PayPal payment yet "
                                 f"(order status {order.get('status')})")
            else:
                auth = self.paypal.authorize_order(t.id, t.payment.order_id)
            p = t.payment
            p.authorization_id, p.authorization_status = auth["authorization_id"], auth["status"]
            p.authorized_at = now()
            p.expiration_time = auth.get("expiration_time") or iso(utcnow() + AUTH_VALIDITY)
            self._record(t, "paypal", "authorized", authorization_id=p.authorization_id,
                         status=p.authorization_status, expiration_time=p.expiration_time)
            self._move(t, State.AUTHORIZED)
            return t

    # --- 4: buyer submits evidence ---

    def submit_evidence(self, tid: str, photo: Optional[ImageInput] = None, note: str = "") -> Threshold:
        with self._locked(tid) as t:
            check(t.state, State.EVIDENCE_SUBMITTED)
            if photo is None and not note.strip():
                raise JanusError("send a photo of the item you received")
            item = EvidenceItem(id=f"V{len(t.evidence) + 1}", note=note.strip()[:1000], submitted_at=now())
            if photo is not None:
                item.file, item.media_type, item.sha256 = self._ingest(t, photo, "evidence", item.id)
            t.evidence.append(item)
            self._record(t, "state", "evidence_added", evidence_id=item.id, has_photo=bool(item.file))
            self._move(t, State.EVIDENCE_SUBMITTED, total_evidence=len(t.evidence))
            return t

    # --- 5 + 6: AI verdict -> policy -> PayPal ---

    def verify(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.state != State.EVIDENCE_SUBMITTED:
                raise TransitionError(f"verify needs new evidence first (state is '{t.state.value}')")
            if self._expired_now(t):
                return t
            verdict = self.ai.verify(t.compiled, t.listing, t.evidence, self.store.dir(t.id), ref=t.id)
            decision = decide(verdict, t.compiled, self.config.min_confidence)
            rec = VerdictRecord(at=now(), evidence_ids=[e.id for e in t.evidence], verdict=verdict,
                                action=decision.action, reason=decision.reason)
            t.verdicts.append(rec)
            self._record(t, "ai", "verdict", verdict=verdict.verdict, confidence=verdict.confidence,
                         decision=decision.action, reason=decision.reason,
                         checks={c.requirement_id: c.result for c in verdict.checks})
            if decision.action == "hold":
                self._move(t, State.HELD, reason=decision.reason)
                return t
            return self._settle(t, decision.action, "janus", f"{verdict.summary} ({decision.reason})")

    def submit_and_verify(self, tid: str, photo: Optional[ImageInput], note: str = "") -> Threshold:
        self.submit_evidence(tid, photo, note)
        return self.verify(tid)

    # --- human decisions (only while funds are held) ---

    def release(self, tid: str, note: str = "") -> Threshold:
        """Buyer releases the money to the seller (capture)."""
        with self._locked(tid) as t:
            check(t.state, State.CAPTURING)
            return self._settle(t, "capture", "buyer", note or "Buyer released the payment")

    def refund(self, tid: str, note: str = "") -> Threshold:
        """Seller releases the hold back to the buyer (void)."""
        with self._locked(tid) as t:
            check(t.state, State.VOIDING)
            return self._settle(t, "void", "seller", note or "Seller refunded the buyer")

    def retry_settlement(self, tid: str) -> Threshold:
        """Re-run a capture or void that failed with a retryable error. Same PayPal-Request-Id."""
        with self._locked(tid) as t:
            if t.state not in (State.CAPTURING, State.VOIDING):
                raise JanusError(f"nothing to retry in state '{t.state.value}'")
            return self._settle(t, "capture" if t.state == State.CAPTURING else "void", None, None)

    # --- PayPal status: expiry and reconciliation ---

    def refresh(self, tid: str) -> Threshold:
        """Ask PayPal for the authorization's live status; mark the threshold expired if it is."""
        with self._locked(tid) as t:
            if t.state in HOLDS_FUNDS and t.state not in (State.CAPTURING, State.VOIDING):
                self._expired_now(t)
            return t

    def sweep(self) -> list[Threshold]:
        return [self.refresh(t.id) for t in self.store.all() if t.state in HOLDS_FUNDS]

    def _sync(self, t: Threshold) -> dict:
        a = self.paypal.get_authorization(t.id, t.payment.authorization_id)
        t.payment.authorization_status = a.get("status")
        t.payment.expiration_time = a.get("expiration_time") or t.payment.expiration_time
        t.payment.checked_at = now()
        return a

    def _expired_now(self, t: Threshold) -> bool:
        try:
            status = self._sync(t).get("status")
        except PayPalError as e:
            t.payment.last_error = str(e)
            self.store.save(t)
            raise
        exp = _parse_ts(t.payment.expiration_time)
        if status == "EXPIRED" or (status == "CREATED" and exp and exp < utcnow()):
            self._finish(t, State.EXPIRED, Outcome(action="expired", by="paypal", at=now(),
                         reason="The PayPal authorization expired before the condition was settled. "
                                "No money moved; the buyer must pay again."))
            return True
        self.store.save(t)
        return False

    def _finish(self, t: Threshold, target: State, outcome: Outcome) -> Threshold:
        t.outcome = outcome
        t.payment.last_error = None
        self._move(t, target, outcome=outcome.action, by=outcome.by)
        return t

    def _settle(self, t: Threshold, action: str, by: Optional[str], reason: Optional[str]) -> Threshold:
        """Capture or void, robust to retries, expiry and work already done on PayPal's side."""
        mid = State.CAPTURING if action == "capture" else State.VOIDING
        if t.state != mid:
            t.outcome = Outcome(action=action, by=by or "janus", reason=reason or "", at=now())
            self._move(t, mid, by=t.outcome.by)
        pending = t.outcome
        p = t.payment

        def done(target: State, why: str = "") -> Threshold:
            if why:
                self._record(t, "paypal", "reconciled", status=p.authorization_status, note=why)
            act = {State.CAPTURED: "capture", State.VOIDED: "void", State.EXPIRED: "expired"}[target]
            return self._finish(t, target, Outcome(action=act, by=pending.by if act == action else "paypal",
                                                   reason=why or pending.reason, at=now()))

        try:
            status = self._sync(t).get("status")
            if status == "EXPIRED":
                return done(State.EXPIRED, "The PayPal authorization had expired. No money moved.")
            if status == "VOIDED":
                return done(State.VOIDED, "" if action == "void" else "The authorization was already voided.")
            if status == "CAPTURED" and action == "void":
                return done(State.CAPTURED, "The payment had already been captured; it cannot be voided.")
            if action == "capture":
                cap = self.paypal.capture_authorization(t.id, p.authorization_id, make_request_id(t.id, "capture"))
                p.capture_id, p.capture_status, p.authorization_status = cap["capture_id"], cap["status"], "CAPTURED"
                self._record(t, "paypal", "captured", capture_id=p.capture_id, status=p.capture_status)
                return done(State.CAPTURED)
            self.paypal.void_authorization(t.id, p.authorization_id, make_request_id(t.id, "void"))
            p.voided, p.authorization_status = True, "VOIDED"
            self._record(t, "paypal", "voided", authorization_id=p.authorization_id)
            return done(State.VOIDED)
        except PayPalError as e:
            issue = e.issue
            p.last_error = f"{issue or 'error'}: {e}"
            if issue == "AUTHORIZATION_EXPIRED":
                return done(State.EXPIRED, "The PayPal authorization had expired. No money moved.")
            if issue in ("AUTHORIZATION_VOIDED", "AUTHORIZATION_ALREADY_VOIDED") and action == "void":
                return done(State.VOIDED)
            if issue == "AUTHORIZATION_VOIDED":
                return done(State.VOIDED, "The authorization was already voided. No money moved.")
            if issue in ("AUTHORIZATION_ALREADY_CAPTURED", "PREVIOUSLY_CAPTURED"):
                return done(State.CAPTURED, "PayPal reports the payment was already captured.")
            if e.retryable:
                self._record(t, "paypal", f"{action}_retry_pending", error=str(e))
                self.store.save(t)
                raise
            self._record(t, "paypal", f"{action}_rejected", issue=issue, error=str(e))
            if action == "capture":  # e.g. INSTRUMENT_DECLINED: money did not move; a human decides
                t.outcome = None
                self._move(t, State.HELD, reason=f"PayPal rejected the capture ({issue or e.status})")
                return t
            self.store.save(t)
            raise
