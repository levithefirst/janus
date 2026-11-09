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
from . import evidence as ev
from .models import EvidenceItem, Listing, RequirementCheck, Verdict
from .clock import iso, parse, utcnow
from .constants import (AUTH_VALIDITY, HONOR_PERIOD, MAX_REAUTHORIZATIONS,
                        VOID_BEFORE_EXPIRY, WARN_BEFORE_HONOR)
from .paypal import PayPalError, make_request_id, PayPalMock, PayPalSandbox, auth_from_order
from .policy import Decision, decide, decide_items
from .state import HOLDS_FUNDS, TERMINAL, State, TransitionError, check
from .rules import RuleViolation
from .security import Security, load_secret, parse_keys
from .store import Amendment, Capture, Consent, Outcome, Pending, Store, Threshold, VerdictRecord

ImageInput = Union[str, Path, bytes]
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000  # 40 MP: above any phone camera
_MAGIC = {b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG": ("image/png", ".png"),
          b"GIF8": ("image/gif", ".gif"), b"RIFF": ("image/webp", ".webp")}


class JanusError(Exception):
    pass


class StaleTermsError(JanusError):
    """The customer agreed to terms that are no longer the current terms."""


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
        probe = Image.open(io.BytesIO(data))  # reads the header only
    except Exception:
        raise JanusError("could not read that file as an image")
    if probe.width * probe.height > MAX_IMAGE_PIXELS:  # decompression bomb: tiny file, huge image
        raise JanusError(f"image is too large ({probe.width}x{probe.height})")
    try:
        img = ImageOps.exif_transpose(probe).convert("RGB")
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
        from .kv import from_config
        self.kv = from_config(config)
        self.store = Store(self.kv, Path(config.data_dir) / "cache")
        self.audit = AuditLog(self.kv)
        self.security = Security(load_secret(config.janus_secret, allow_ephemeral=config.paypal_mode == "mock"),
                                 parse_keys(config.tenant_keys), config.admin_token)
        self.phash_index = ev.PhashIndex(self.kv)
        from .webhooks import Inbox
        self.inbox = Inbox(self.kv)
        self.ai = ai or (ClaudeAI(config.model) if config.ai_mode == "claude" else MockAI())
        self.paypal = paypal or (
            PayPalSandbox(config.paypal_client_id, config.paypal_client_secret,
                          negative_test=config.paypal_negative_test)
            if config.paypal_mode == "sandbox" else PayPalMock(self.kv, config.paypal_negative_test))

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
        self.store.put_file(t.id, f"{subdir}/{stem}{ext}", data)
        return f"{subdir}/{stem}{ext}", media_type, hashlib.sha256(data).hexdigest()

    def get(self, tid: str) -> Threshold:
        return self.store.load(tid)

    def token(self, t: Threshold, role: str) -> str:
        return self.security.token(t.tenant, t.id, role)

    def buyer_link(self, t: Threshold) -> str:
        return f"{self.config.base_url}/t/{t.id}?t={self.token(t, 'buyer')}"

    def seller_link(self, t: Threshold) -> str:
        return f"{self.config.base_url}/t/{t.id}?k={self.token(t, 'seller')}"

    def role_for(self, t: Threshold, presented: Optional[str]) -> Optional[str]:
        return self.security.role_for(t.tenant, t.id, presented)

    def is_seller(self, t: Threshold, key: Optional[str]) -> bool:
        return self.role_for(t, key) == "seller"

    @staticmethod
    def honor_period_over(t: Threshold) -> bool:
        at = _parse_ts(t.payment.authorized_at)
        return bool(at) and utcnow() > at + HONOR_PERIOD

    # --- 1 + 2: seller states the condition, AI compiles it ---

    def create(self, title: str, price: str, condition: str, description: str = "",
               currency: str = "USD", photos: Optional[list[ImageInput]] = None, tenant: str = "public") -> Threshold:
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
        t = Threshold(id=tid, tenant=tenant, state=State.CREATED, created_at=now(),
                      updated_at=now(), listing=listing, condition=condition, compiled=compiled)
        self._record(t, "ai", "condition_compiled", requirements=[f"{r.id}{'*' if r.critical else ''}"
                                                                  for r in compiled.requirements])
        self._record(t, "state", "created", state=State.CREATED.value)
        self.store.save(t)
        self._audit(t, "condition_compiled", "seller", "seller stated the condition; AI compiled it",
                    requirements=[r.model_dump() for r in compiled.requirements], terms_hash=rules.terms_hash(t))
        return t

    # --- itemized pricing and scope amendments (provider proposes, customer consents) ---

    def set_allocations(self, tid: str, allocations: dict[str, str]) -> Threshold:
        """Split the price across checklist items so each can be captured on its own. Before payment only."""
        with self._locked(tid) as t:
            if t.state != State.CREATED:
                raise TransitionError("allocations are part of the terms: set them before the customer pays")
            ids = {r.id for r in t.compiled.requirements}
            if set(allocations) != ids:
                raise JanusError(f"allocate every checklist item exactly once: {sorted(ids)}")
            amounts = {k: _money(v) for k, v in allocations.items()}
            if sum(Decimal(v) for v in amounts.values()) != Decimal(t.amount):
                raise JanusError(f"allocations must add up to the price {t.amount}")
            t.allocations, t.item_status = amounts, {k: "pending" for k in amounts}
            self.store.save(t)
            self._audit(t, "allocations_set", "seller", "price split per checklist item", allocations=amounts,
                        terms_hash=rules.terms_hash(t))
            return t

    def propose_amendment(self, tid: str, condition: Optional[str] = None, price: Optional[str] = None,
                          reason: str = "") -> Threshold:
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            if t.state in (State.CAPTURING, State.VOIDING):
                raise TransitionError("a settlement is in progress")
            if t.allocations:
                raise JanusError("itemized thresholds cannot be amended yet (open item)")
            if condition is None and price is None:
                raise JanusError("an amendment changes the condition, the price, or both")
            new_price = _money(price) if price is not None else None
            if new_price and t.payment.authorized_amount and Decimal(new_price) > Decimal(t.payment.authorized_amount):
                raise JanusError(f"{new_price} is more than the {t.payment.authorized_amount} the customer "
                                 "authorized; that needs a new authorization")
            if new_price and Decimal(new_price) < self._captured_total(t):
                raise JanusError("the new price is below what has already been captured")
            t.amendment = Amendment(condition=(condition or "").strip() or None, price=new_price,
                                    reason=reason[:500], at=now())
            self.store.save(t)
            self._audit(t, "amendment_proposed", "seller", reason or "scope change proposed",
                        condition=t.amendment.condition, price=new_price)
            return t

    def accept_amendment(self, tid: str, channel: str = "web") -> Threshold:
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            a = t.amendment
            if a is None:
                raise JanusError("no amendment is waiting")
            if a.condition:
                t.condition = a.condition
                t.compiled = self.ai.compile_condition(a.condition, t.listing, self.store.dir(t.id), ref=t.id)
            if a.price:
                t.listing.price = a.price
            t.amendment = None
            c = Consent(kind="amendment", by="buyer", at=now(), terms_hash=rules.terms_hash(t), amount=t.amount,
                        channel=channel)
            t.consents.append(c)
            self.store.save(t)
            self._audit(t, "amendment_accepted", "buyer", "customer consented to the amended terms",
                        terms_hash=c.terms_hash, amount=t.amount, condition=t.condition)
            return t

    def reject_amendment(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.amendment is None:
                raise JanusError("no amendment is waiting")
            t.amendment = None
            self.store.save(t)
            self._audit(t, "amendment_rejected", "buyer", "customer kept the original terms")
            return t

    # --- explicit customer consent ---

    def consent(self, tid: str, channel: str = "web", evidence_from: Optional[list[str]] = None,
                terms_hash: Optional[str] = None) -> Threshold:
        """The customer agrees, for the current terms, that a verified condition may capture automatically.

        `terms_hash` is the hash of the terms the customer was shown. If it is given and no longer matches
        the current terms (the provider changed them in between), nothing is recorded.
        """
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            if terms_hash is not None and terms_hash != rules.terms_hash(t):
                self._audit(t, "consent_refused", "buyer", "the terms changed after the customer was shown them",
                            channel=channel)
                raise StaleTermsError("the terms changed after you opened this page; reload it and read them again")
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
                                             return_url=f"{base}/paypal/return", cancel_url=f"{base}/",
                                             request_id=make_request_id(t.id, "create"))
            t.payment.order_id, t.payment.approve_url = order["order_id"], order["approve_url"]
            self._record(t, "paypal", "order_created", order_id=order["order_id"])
            self._move(t, State.PAYMENT_PENDING)
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
            p.authorized_amount = t.amount
            p.expiration_time = auth.get("expiration_time") or iso(utcnow() + AUTH_VALIDITY)
            self._record(t, "paypal", "authorized", authorization_id=p.authorization_id,
                         status=p.authorization_status, expiration_time=p.expiration_time)
            self._move(t, State.AUTHORIZED)
            self._audit(t, "authorized", "buyer", "PayPal authorization created; funds held",
                        authorization_id=p.authorization_id, expiration_time=p.expiration_time)
            return t

    # --- 4: buyer submits evidence ---

    def submit_evidence(self, tid: str, photo: Optional[ImageInput] = None, note: str = "",
                        by: str = "buyer") -> Threshold:
        """The customer (or, on re-upload, the provider) submits a photo. Integrity checks run on the
        original bytes before the photo is re-encoded."""
        if by not in ("buyer", "seller"):
            raise JanusError("evidence comes from the buyer or the seller")
        with self._locked(tid) as t:
            rules.check_not_terminal(t)
            in_dispute = t.state == State.DISPUTED
            if in_dispute and by != "seller":
                raise TransitionError("during a dispute only the provider adds evidence; withdraw the dispute first")
            if not in_dispute:
                check(t.state, State.EVIDENCE_SUBMITTED)
            clean = ev.sanitize_note(note)
            if photo is None and not clean:
                raise JanusError("send a photo of the item")
            item = EvidenceItem(id=f"V{len(t.evidence) + 1}", by=by, note=clean, submitted_at=now(),
                                flags=ev.note_flags(clean))
            if photo is not None:
                raw = Path(photo).read_bytes() if isinstance(photo, (str, Path)) else photo
                normalize_image(raw)  # validates the type and size before any analysis
                info = ev.analyze(raw)
                item.quality = {k: info[k] for k in ("width", "height", "sharpness", "brightness")} | {
                    "ok": info["quality_ok"], "reasons": info["quality_reasons"]}
                item.exif, item.phash = info["exif"], info["phash"]
                item.file, item.media_type, item.sha256 = self._ingest(t, raw, "evidence", item.id)
                for hit in self.phash_index.add_and_match(t.id, item.id, item.phash):
                    item.flags.append(f"reused_photo:{hit['threshold_id']}/{hit['evidence_id']}")
                for lp in t.listing.photos:
                    lh = ev.phash_of_file(self.store.dir(t.id) / lp)
                    if lh and ev.hamming(lh, item.phash) <= ev.REUSE_MAX_DISTANCE:
                        item.flags.append("matches_listing_photo")
            t.evidence.append(item)
            self._record(t, "state", "evidence_added", evidence_id=item.id, by=by, has_photo=bool(item.file),
                         flags=item.flags, quality_ok=item.quality.get("ok"))
            self._audit(t, "evidence_added", by, f"evidence {item.id} submitted", evidence_id=item.id,
                        flags=item.flags, quality=item.quality, exif_present=bool(item.exif.get("present")))
            if in_dispute:
                self.store.save(t)
            else:
                self._move(t, State.EVIDENCE_SUBMITTED, total_evidence=len(t.evidence))
            return t

    # --- 5 + 6: AI verdict -> policy -> PayPal ---

    def verify(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.state != State.EVIDENCE_SUBMITTED:
                raise TransitionError(f"verify needs new evidence first (state is '{t.state.value}')")
            if self._expired_now(t):
                return t
            newest = next((e for e in reversed(t.evidence) if e.file), None)
            evidence_by = t.evidence[-1].by if t.evidence else "buyer"
            trust = ev.assess(t.evidence, t.payment.authorized_at, t.delivery_location)
            bar = trust.bar(self.config.min_confidence)
            if newest is not None and not newest.quality.get("ok", True):
                return self._gate_hold(t, "quality_gate", newest.quality.get("reasons", []), bar, trust, evidence_by,
                                       "Retake the photo in good light, in focus, with the whole item in frame.")
            if trust.hold_reasons:
                return self._gate_hold(t, "integrity", trust.hold_reasons, bar, trust, evidence_by,
                                       "A person needs to review this evidence before any money moves.")
            verdict = self.ai.verify(t.compiled, t.listing, t.evidence, self.store.dir(t.id), ref=t.id)
            consent = rules.valid_consent(t, ("auto_capture_on_verified", "amendment"))
            may_capture = consent is not None and evidence_by in consent.evidence_from
            wait = (f"verified on {evidence_by} evidence; waiting for the customer to confirm "
                    "(a silent customer never triggers a capture)")
            items = None
            if t.allocations:
                pending = [r for r, st in t.item_status.items() if st == "pending"]
                items = decide_items(verdict, pending, bar)
                cap = items.capture if may_capture else []
                rest = [r for r in pending if r not in cap and r not in items.release]
                if cap:
                    decision = Decision("capture", items.reason)
                elif items.release and not rest:
                    decision = Decision("void", items.reason)
                else:
                    decision = Decision("hold", items.reason if may_capture or not items.capture else wait)
            else:
                decision = decide(verdict, t.compiled, bar)
                if decision.action == "capture" and not may_capture:
                    decision = Decision("hold", f"condition {wait}")
            rec = VerdictRecord(at=now(), evidence_ids=[e.id for e in t.evidence], verdict=verdict,
                                action=decision.action, reason=decision.reason, min_confidence=bar,
                                evidence_by=evidence_by, trust_flags=trust.flags)
            t.verdicts.append(rec)
            self._record(t, "ai", "verdict", verdict=verdict.verdict, confidence=verdict.confidence,
                         decision=decision.action, reason=decision.reason, bar=bar, flags=trust.flags,
                         checks={c.requirement_id: c.result for c in verdict.checks})
            self._audit(t, "verdict", "janus", decision.reason, verdict=verdict.verdict,
                        confidence=verdict.confidence, min_confidence=bar, decision=decision.action,
                        trust_flags=trust.flags, evidence_by=evidence_by,
                        checks={c.requirement_id: [c.result, c.observation] for c in verdict.checks})
            if decision.action == "hold" and items is None:
                self._move(t, State.HELD, reason=decision.reason)
                return t
            reason = f"{verdict.summary} ({decision.reason})"
            if items is not None:
                for rid in items.release:
                    t.item_status[rid] = "released"
                if decision.action == "capture":
                    cap = items.capture
                    remaining = [r for r, st in t.item_status.items() if st == "pending" and r not in cap]
                    amount = str(sum(Decimal(t.allocations[r]) for r in cap))
                    return self._start(t, "capture", "janus", reason, amount=amount, final=not remaining,
                                       items=tuple(cap), verdict_index=len(t.verdicts) - 1)
                if decision.action == "hold":
                    self._move(t, State.HELD, reason=decision.reason)
                    return t
            if decision.action == "capture":
                return self._start(t, "capture", "janus", reason, verdict_index=len(t.verdicts) - 1)
            return self._start(t, "void", "janus", reason)

    def _gate_hold(self, t: Threshold, source: str, reasons: list[str], bar: float, trust, evidence_by: str,
                   next_step: str) -> Threshold:
        """Hold without asking the AI: the evidence itself is not fit to decide on."""
        why = "; ".join(reasons)
        verdict = Verdict(verdict="uncertain", confidence=0.0, summary=f"Janus did not judge this evidence: {why}.",
                          next_step=next_step, checks=[RequirementCheck(requirement_id=r.id, result="cannot_tell",
                                                                        observation=f"not assessed ({source})")
                                                       for r in t.compiled.requirements])
        t.verdicts.append(VerdictRecord(at=now(), evidence_ids=[e.id for e in t.evidence], verdict=verdict,
                                        action="hold", reason=f"{source}: {why}", min_confidence=bar,
                                        evidence_by=evidence_by, source=source, trust_flags=trust.flags))
        self._audit(t, "verdict", source, why, decision="hold", trust_flags=trust.flags)
        self._move(t, State.HELD, reason=f"{source}: {why}")
        return t

    def submit_and_verify(self, tid: str, photo: Optional[ImageInput], note: str = "",
                          by: str = "buyer") -> Threshold:
        self.submit_evidence(tid, photo, note, by=by)
        return self.verify(tid)

    # --- human decisions (only while funds are held) ---

    def release(self, tid: str, note: str = "", channel: str = "web") -> Threshold:
        """The customer releases the held money to the provider (capture). This is explicit consent."""
        with self._locked(tid) as t:
            check(t.state, State.CAPTURING)
            pending = [r for r, st in t.item_status.items() if st == "pending"]
            if t.allocations and not pending:
                # Nothing is left to capture: never call PayPal with an amount of zero.
                if all(st == "captured" for st in t.item_status.values()):
                    self._audit(t, "release_noop", "buyer", "release asked, but every item is already captured",
                                channel=channel)
                    return t
                raise JanusError("there is nothing left to capture: every item is already captured or released")
            t.consents.append(Consent(kind="release", by="buyer", at=now(), terms_hash=rules.terms_hash(t),
                                      amount=t.amount, channel=channel))
            self._audit(t, "consent", "buyer", "customer released the payment", consent_kind="release",
                        channel=channel)
            if t.allocations:
                amount = str(sum(Decimal(t.allocations[r]) for r in pending))
                return self._start(t, "capture", "buyer", note or "Buyer released the payment", amount=amount,
                                   items=tuple(pending))
            return self._start(t, "capture", "buyer", note or "Buyer released the payment")

    def refund(self, tid: str, note: str = "") -> Threshold:
        """The provider releases the hold back to the customer (void)."""
        with self._locked(tid) as t:
            check(t.state, State.VOIDING)
            return self._start(t, "void", "seller", note or "Seller refunded the buyer")

    def dispute(self, tid: str, reason: str) -> Threshold:
        """The customer objects while funds are held. Automatic settlement stops until it is resolved."""
        if not reason.strip():
            raise JanusError("a dispute needs a reason")
        with self._locked(tid) as t:
            check(t.state, State.DISPUTED)
            self._move(t, State.DISPUTED, reason=reason[:500])
            self._audit(t, "dispute_opened", "buyer", reason[:500])
            return t

    def withdraw_dispute(self, tid: str) -> Threshold:
        with self._locked(tid) as t:
            if t.state != State.DISPUTED:
                raise TransitionError(f"no open dispute (state is '{t.state.value}')")
            self._move(t, State.HELD, reason="dispute withdrawn")
            self._audit(t, "dispute_withdrawn", "buyer", "customer withdrew the dispute")
            return t

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

    # --- expiry scheduler (see scheduler.py) ---

    def expiry_tick(self, tid: str) -> list[str]:
        """One scheduler step for one threshold. Warns, reauthorizes, asks for a new authorization,
        or voids before day 29. It never captures: a silent customer never leads to a capture."""
        with self._locked(tid) as t:
            if t.state not in HOLDS_FUNDS or t.state in (State.CAPTURING, State.VOIDING):
                return []
            p, now_ = t.payment, utcnow()
            authorized_at = _parse_ts(p.authorized_at)
            original = _parse_ts(p.original_authorized_at) or authorized_at
            if authorized_at is None:
                return []
            expiry = original + AUTH_VALIDITY
            if (paypal_exp := _parse_ts(p.expiration_time)) is not None:
                expiry = min(expiry, paypal_exp)
            if now_ >= expiry - VOID_BEFORE_EXPIRY:
                self._start(t, "void", "scheduler", "The authorization is about to expire and the condition was "
                                                    "not settled. Voided so the customer's funds are released.")
                return ["voided_before_expiry"]
            if now_ >= authorized_at + HONOR_PERIOD:
                return self._after_honor(t)
            if now_ >= authorized_at + HONOR_PERIOD - WARN_BEFORE_HONOR and not p.honor_warned:
                p.honor_warned = True
                self.store.save(t)
                self._audit(t, "honor_period_warning", "scheduler", "the 3-day PayPal honor period ends soon",
                            honor_ends=iso(authorized_at + HONOR_PERIOD))
                return ["warned"]
            return []

    def _after_honor(self, t: Threshold) -> list[str]:
        p = t.payment
        if p.needs_new_authorization:
            return []
        if p.reauthorize_count < MAX_REAUTHORIZATIONS and not p.captures:
            rid = make_request_id(t.id, "reauthorize", p.reauthorize_count + 1)
            try:
                r = self.paypal.reauthorize(t.id, p.authorization_id, rid)
            except PayPalError as e:
                if e.retryable:
                    p.last_error = str(e)
                    self.store.save(t)
                    return ["reauthorize_retry_pending"]
                self._audit(t, "reauthorize_failed", "scheduler", str(e), issue=e.issue)
            else:
                p.previous_authorization_ids.append(p.authorization_id)
                p.authorization_id, p.authorization_status = r["authorization_id"], r["status"]
                p.authorized_at, p.reauthorize_count, p.honor_warned = now(), p.reauthorize_count + 1, False
                p.expiration_time = r.get("expiration_time") or p.expiration_time
                self.store.save(t)
                self._audit(t, "reauthorized", "scheduler", "honor period over; funds reauthorized",
                            request_id=rid, authorization_id=p.authorization_id,
                            previous=p.previous_authorization_ids[-1])
                return ["reauthorized"]
        p.needs_new_authorization = True
        self.store.save(t)
        self._audit(t, "new_authorization_needed", "scheduler",
                    "honor period over and reauthorization is not possible; ask the customer to authorize again",
                    severity="high", reauthorize_count=p.reauthorize_count)
        return ["new_authorization_requested"]

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
            if Decimal(amount) <= 0:  # last line of defence: PayPal is never asked to capture zero or less
                raise JanusError(f"nothing to capture (amount {amount})")
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

        if action == "capture" and Decimal(pend.amount or "0") <= 0:
            self._audit(t, "rule_blocked", pend.by, "capture amount is zero or negative", rule="amount",
                        request_id=pend.request_id)
            return self._hold(t, "Blocked: the capture amount is not positive", actor="rules")
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
                for rid in pend.items:
                    t.item_status[rid] = "captured"
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
