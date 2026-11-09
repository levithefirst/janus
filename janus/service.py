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

from . import rules
from .ai import ClaudeAI, MockAI
from .audit import AuditLog
from .config import Config
from .logs import log_event, now, setup
from .models import EvidenceItem, Listing
from .clock import iso, parse, utcnow
from .constants import AUTH_VALIDITY, HONOR_PERIOD
from .paypal import PayPalError, make_request_id, PayPalMock, PayPalSandbox, auth_from_order
from .policy import decide
from .state import HOLDS_FUNDS, TERMINAL, State, TransitionError, check
from .rules import RuleViolation
from .store import Capture, Consent, Outcome, Pending, Store, Threshold, VerdictRecord

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
        self.audit = AuditLog(config.data_dir)
        from .webhooks import Inbox
        self.inbox = Inbox(config.data_dir)
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

    def _audit(self, t: Threshold, kind: str, actor: str, reason: str, **data) -> None:
        self.audit.append(kind, t.id, actor, reason, state=t.state.value, **data)

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
        self._audit(t, "condition_compiled", "seller", "seller stated the condition; AI compiled it",
                    requirements=[r.model_dump() for r in compiled.requirements], terms_hash=rules.terms_hash(t))
        return t

    # --- explicit customer consent ---

    def consent(self, tid: str, channel: str = "web", evidence_from: Optional[list[str]] = None) -> Threshold:
        """The customer agrees, for the current terms, that a verified condition may capture automatically."""
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            c = Consent(kind="auto_capture_on_verified", by="buyer", at=now(), terms_hash=rules.terms_hash(t),
                        amount=t.amount, channel=channel, evidence_from=evidence_from or ["buyer"])
            t.consents.append(c)
            self._record(t, "state", "consent_recorded", kind=c.kind, channel=channel)
            self.store.save(t)
            self._audit(t, "consent", "buyer", "customer agreed to capture on a verified condition",
                        terms_hash=c.terms_hash, amount=c.amount, channel=channel, evidence_from=c.evidence_from)
            return t

    # --- 3: buyer authorizes on PayPal (money held, not taken) ---

    def create_payment(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            if t.state != State.CREATED:  # idempotent: re-issue the same order
                if not t.payment.order_id:
                    check(t.state, State.PAYMENT_PENDING)
                return t
            if not rules.valid_consent(t, ("auto_capture_on_verified", "amendment")):
                raise JanusError("the customer must consent to the condition before paying")
            base = self.config.base_url
            order = self.paypal.create_order(t.id, t.amount, t.currency, t.listing.title,
                                             return_url=f"{base}/paypal/return", cancel_url=f"{base}/t/{t.id}",
                                             request_id=make_request_id(t.id, "create"))
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
                auth = self.paypal.authorize_order(t.id, t.payment.order_id, make_request_id(t.id, "authorize"))
            p = t.payment
            p.authorization_id, p.authorization_status = auth["authorization_id"], auth["status"]
            p.authorized_at = now()
            p.original_authorized_at = p.original_authorized_at or p.authorized_at
            p.expiration_time = auth.get("expiration_time") or iso(utcnow() + AUTH_VALIDITY)
            self._record(t, "paypal", "authorized", authorization_id=p.authorization_id,
                         status=p.authorization_status, expiration_time=p.expiration_time)
            self._move(t, State.AUTHORIZED)
            self._audit(t, "authorized", "buyer", "PayPal authorization created; funds held",
                        authorization_id=p.authorization_id, expiration_time=p.expiration_time)
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
            bar = self.config.min_confidence
            decision = decide(verdict, t.compiled, bar)
            rec = VerdictRecord(at=now(), evidence_ids=[e.id for e in t.evidence], verdict=verdict,
                                action=decision.action, reason=decision.reason, min_confidence=bar)
            t.verdicts.append(rec)
            self._record(t, "ai", "verdict", verdict=verdict.verdict, confidence=verdict.confidence,
                         decision=decision.action, reason=decision.reason,
                         checks={c.requirement_id: c.result for c in verdict.checks})
            self._audit(t, "verdict", "janus", decision.reason, verdict=verdict.verdict,
                        confidence=verdict.confidence, min_confidence=bar, decision=decision.action,
                        checks={c.requirement_id: [c.result, c.observation] for c in verdict.checks})
            if decision.action == "hold":
                self._move(t, State.HELD, reason=decision.reason)
                return t
            reason = f"{verdict.summary} ({decision.reason})"
            if decision.action == "capture":
                return self._start(t, "capture", "janus", reason, verdict_index=len(t.verdicts) - 1)
            return self._start(t, "void", "janus", reason)

    def submit_and_verify(self, tid: str, photo: Optional[ImageInput], note: str = "") -> Threshold:
        self.submit_evidence(tid, photo, note)
        return self.verify(tid)

    # --- human decisions (only while funds are held) ---

    def release(self, tid: str, note: str = "", channel: str = "web") -> Threshold:
        """The customer releases the held money to the provider (capture). This is explicit consent."""
        with self._locked(tid) as t:
            check(t.state, State.CAPTURING)
            t.consents.append(Consent(kind="release", by="buyer", at=now(), terms_hash=rules.terms_hash(t),
                                      amount=t.amount, channel=channel))
            self._audit(t, "consent", "buyer", "customer released the payment", consent_kind="release",
                        channel=channel)
            return self._start(t, "capture", "buyer", note or "Buyer released the payment")

    def refund(self, tid: str, note: str = "") -> Threshold:
        """The provider releases the hold back to the customer (void)."""
        with self._locked(tid) as t:
            check(t.state, State.VOIDING)
            return self._start(t, "void", "seller", note or "Seller refunded the buyer")

    def retry_settlement(self, tid: str) -> Threshold:
        """Re-run a capture or void that failed with a retryable error. Same PayPal-Request-Id."""
        with self._locked(tid) as t:
            if t.state not in (State.CAPTURING, State.VOIDING) or not t.pending:
                raise JanusError(f"nothing to retry in state '{t.state.value}'")
            return self._settle(t)

    def reconcile(self, tid: str) -> Threshold:
        """Bring a threshold in line with PayPal's own record. Safe to call any time, any number of times.

        After a crash mid-capture the threshold is in `capturing` with the exact request on disk:
        replaying it with the same PayPal-Request-Id either completes it or returns the capture
        PayPal already made. Never a second capture.
        """
        with self._locked(tid) as t:
            if t.state in (State.CAPTURING, State.VOIDING) and t.pending:
                try:
                    return self._settle(t)
                except PayPalError:
                    return t
            if t.state in HOLDS_FUNDS and t.payment.authorization_id:
                self._expired_now(t, reconcile=True)
            return t

    def recover(self) -> list[Threshold]:
        """Run at startup and on every scheduler tick: finish settlements interrupted by a crash."""
        return [self.reconcile(t.id) for t in self.store.all() if t.state in (State.CAPTURING, State.VOIDING)]

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

    def _expired_now(self, t: Threshold, reconcile: bool = False) -> bool:
        try:
            status = self._sync(t).get("status")
        except PayPalError as e:
            t.payment.last_error = str(e)
            self.store.save(t)
            raise
        exp = _parse_ts(t.payment.expiration_time)
        if status == "EXPIRED" or (status in ("CREATED", "PARTIALLY_CAPTURED") and exp and exp <= utcnow()):
            self._finish(t, State.EXPIRED, Outcome(action="expired", by="paypal", at=now(),
                         reason="The PayPal authorization expired before the condition was settled. "
                                "No money moved; the buyer must pay again."))
            return True
        if reconcile and status == "VOIDED":
            self._move(t, State.VOIDING, by="paypal", reconcile=True)
            self._finish(t, State.VOIDED, Outcome(action="void", by="paypal", at=now(),
                         reason="PayPal reports the authorization was voided outside Janus."))
            return True
        if reconcile and status == "CAPTURED":
            self._move(t, State.CAPTURING, by="paypal", reconcile=True)
            self._finish(t, State.CAPTURED, Outcome(action="capture", by="paypal", at=now(),
                         reason="PayPal reports the authorization was captured outside Janus."))
            self._audit(t, "alert", "paypal", "capture found that Janus did not make", severity="high")
            return True
        self.store.save(t)
        return False

    def _finish(self, t: Threshold, target: State, outcome: Outcome) -> Threshold:
        t.outcome = outcome
        t.pending = None
        t.payment.last_error = None
        if target in (State.VOIDED, State.EXPIRED):
            t.ever_voided_or_expired = True
        self._move(t, target, outcome=outcome.action, by=outcome.by)
        self._audit(t, target.value, outcome.by, outcome.reason, capture_ids=[c.capture_id for c in t.payment.captures])
        return t

    def _captured_total(self, t: Threshold) -> Decimal:
        return sum((Decimal(c.amount) for c in t.payment.captures), Decimal("0"))

    def _start(self, t: Threshold, action: str, by: str, reason: str, amount: Optional[str] = None,
               final: bool = True, items: tuple[str, ...] = (), verdict_index: Optional[int] = None) -> Threshold:
        """Write the exact settlement to disk (state + request id + amount), then execute it."""
        mid = State.CAPTURING if action == "capture" else State.VOIDING
        if action == "capture":
            amount = amount or str(Decimal(t.amount) - self._captured_total(t))
            rid = make_request_id(t.id, "capture", len(t.payment.captures) + 1)
        else:
            rid = make_request_id(t.id, "void")
        t.pending = Pending(action=action, by=by, reason=reason, request_id=rid, amount=amount, final=final,
                            items=list(items), verdict_index=verdict_index, at=now())
        t.outcome = Outcome(action=action, by=by, reason=reason, at=now())
        self._move(t, mid, by=by, request_id=rid)
        self._audit(t, f"{action}_requested", by, reason, request_id=rid, amount=amount, final=final,
                    items=list(items))
        return self._settle(t)

    def _hold(self, t: Threshold, reason: str, actor: str = "janus") -> Threshold:
        t.pending, t.outcome = None, None
        self._move(t, State.HELD, reason=reason)
        self._audit(t, "hold", actor, reason)
        return t

    def _settle(self, t: Threshold) -> Threshold:
        """Execute t.pending, robust to retries, crashes, expiry and work already done on PayPal's side."""
        pend, p = t.pending, t.payment
        action = pend.action

        def done(target: State, why: str = "") -> Threshold:
            if why:
                self._record(t, "paypal", "reconciled", status=p.authorization_status, detail=why)
            act = {State.CAPTURED: "capture", State.VOIDED: "void", State.EXPIRED: "expired"}[target]
            return self._finish(t, target, Outcome(action=act, by=pend.by if act == action else "paypal",
                                                   reason=why or pend.reason, at=now()))

        if action == "capture":
            try:
                rules.check_capture(t, pend.by, self.config.min_confidence)
            except RuleViolation as v:
                self._audit(t, "rule_blocked", pend.by, str(v), rule=v.rule, request_id=pend.request_id)
                return self._hold(t, f"Blocked by rule {v.rule}: {v}", actor="rules")
        try:
            status = self._sync(t).get("status")
            if status == "EXPIRED":
                return done(State.EXPIRED, "The PayPal authorization had expired. No money moved.")
            if status == "VOIDED":
                return done(State.VOIDED, "" if action == "void" else "The authorization was already voided.")
            if status == "CAPTURED" and action == "void":
                return done(State.CAPTURED, "The payment had already been captured; it cannot be voided.")
            if action == "capture":
                cap = self.paypal.capture_authorization(t.id, p.authorization_id, pend.request_id,
                                                        amount=pend.amount, currency=t.currency,
                                                        final_capture=pend.final)
                if not any(c.capture_id == cap["capture_id"] for c in p.captures):
                    p.captures.append(Capture(capture_id=cap["capture_id"], amount=cap.get("amount") or pend.amount,
                                              request_id=pend.request_id, final=pend.final, items=pend.items,
                                              at=now()))
                p.capture_id, p.capture_status = cap["capture_id"], cap["status"]
                p.authorization_status = "CAPTURED" if pend.final else "PARTIALLY_CAPTURED"
                self._record(t, "paypal", "captured", capture_id=p.capture_id, amount=pend.amount, final=pend.final)
                if pend.final:
                    return done(State.CAPTURED)
                self._audit(t, "partial_capture", pend.by, pend.reason, capture_id=p.capture_id,
                            amount=pend.amount, items=pend.items)
                return self._hold(t, "Partial capture done; remaining items still held", actor=pend.by)
            self.paypal.void_authorization(t.id, p.authorization_id, pend.request_id)
            p.voided, p.authorization_status = True, "VOIDED"
            self._record(t, "paypal", "voided", authorization_id=p.authorization_id)
            return done(State.VOIDED)
        except PayPalError as e:
            issue = e.issue
            p.last_error = f"{issue or 'error'}: {e}"
            if issue == "AUTHORIZATION_EXPIRED":
                return done(State.EXPIRED, "The PayPal authorization had expired. No money moved.")
            if issue in ("AUTHORIZATION_VOIDED", "AUTHORIZATION_ALREADY_VOIDED"):
                return done(State.VOIDED, "" if action == "void" else "The authorization was already voided.")
            if issue in ("AUTHORIZATION_ALREADY_CAPTURED", "PREVIOUSLY_CAPTURED"):
                return done(State.CAPTURED, "PayPal reports the payment was already captured.")
            if e.retryable:
                self._record(t, "paypal", f"{action}_retry_pending", error=str(e))
                self._audit(t, f"{action}_retry_pending", pend.by, str(e), request_id=pend.request_id)
                self.store.save(t)
                raise
            self._record(t, "paypal", f"{action}_rejected", issue=issue, error=str(e))
            self._audit(t, f"{action}_rejected", "paypal", str(e), issue=issue)
            if action == "capture":  # e.g. INSTRUMENT_DECLINED: money did not move; a human decides
                return self._hold(t, f"PayPal rejected the capture ({issue or e.status})", actor="paypal")
            self.store.save(t)
            raise
