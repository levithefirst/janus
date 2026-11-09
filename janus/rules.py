"""Hard rules. Checked immediately before every PayPal capture, independently of the policy.

The policy decides what *should* happen; these rules decide what *may* happen. A bug in the
policy, the AI, a webhook handler or the scheduler cannot capture money if a rule says no.

1. No capture without a recorded, explicit customer consent bound to the current terms.
2. An automatic capture needs a verdict whose confidence clears the bar it was judged against.
3. Never capture after a void or an expiry, or past the authorization's expiry time.
4. Nothing changes a threshold in a terminal state (also enforced by Store.save).
5. A silent customer never triggers a capture: the scheduler and webhooks cannot capture.
6. No automatic capture while a dispute is open or an amendment is waiting for consent.
7. Job mode: only the customer can capture; the AI's acceptance packet is advice.
8. Job mode: a packet with blocking flags (reused or edited photo, wrong room, no AI packet, and so on)
   cannot be captured unless the customer recorded an override, with a reason, for that exact packet.
"""
from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .clock import parse, utcnow
from .state import TERMINAL, State

if TYPE_CHECKING:
    from .store import Threshold

AUTO = "janus"                     # the AI + policy path
HUMAN_CAPTURERS = {"buyer"}        # only the customer can capture by hand
NEVER_CAPTURE = {"scheduler", "webhook", "paypal", "system", "seller"}


class RuleViolation(Exception):
    def __init__(self, rule: str, message: str):
        super().__init__(f"{rule}: {message}")
        self.rule = rule


def terms_hash(t: "Threshold") -> str:
    terms = {"condition": t.condition, "price": t.listing.price, "currency": t.listing.currency,
             "requirements": [(r.id, r.statement, r.critical) for r in t.compiled.requirements],
             "allocations": dict(sorted(t.allocations.items()))}
    return hashlib.sha256(json.dumps(terms, sort_keys=True).encode()).hexdigest()


def valid_consent(t: "Threshold", kinds: tuple[str, ...]):
    h = terms_hash(t)
    for c in reversed(t.consents):
        if c.kind in kinds and c.terms_hash == h and c.by == "buyer":
            return c
    return None


def check_not_terminal(t: "Threshold") -> None:
    if t.state in TERMINAL:
        raise RuleViolation("terminal", f"{t.id} is {t.state.value}; nothing can change it")


def check_capture(t: "Threshold", by: str, min_confidence_floor: float) -> None:
    """Raise RuleViolation unless capturing now is allowed. `by` is who initiated the capture."""
    check_not_terminal(t)
    p = t.payment
    # 3. never after a void or expiry
    if t.ever_voided_or_expired or p.voided or p.authorization_status in ("VOIDED", "EXPIRED"):
        raise RuleViolation("no_capture_after_void_or_expiry", "the authorization was voided or expired")
    exp = parse(p.expiration_time)
    if exp is not None and utcnow() >= exp:
        raise RuleViolation("no_capture_after_void_or_expiry", "the authorization has expired")
    # 5. silent customer: nothing but the AI path or the customer can capture
    if by in NEVER_CAPTURE or (by != AUTO and by not in HUMAN_CAPTURERS):
        raise RuleViolation("silent_customer", f"'{by}' may not capture; only a verified condition "
                                               "or the customer can")
    if t.kind == "job":
        # 7. job mode: only the customer decides; the AI's packet never moves money
        if by not in HUMAN_CAPTURERS:
            raise RuleViolation("customer_decides", "in a job only the customer can capture")
        # 8. a flagged packet cannot be captured without an explicit, recorded customer override
        if t.verdicts and t.verdicts[-1].blocking_flags:
            idx = len(t.verdicts) - 1
            h = terms_hash(t)
            if not any(c.kind == "override" and c.verdict_index == idx and c.terms_hash == h and c.by == "buyer"
                       and c.reason.strip() for c in t.consents):
                raise RuleViolation("flags_need_override", "the packet is flagged ("
                                    + ", ".join(t.verdicts[-1].blocking_flags) + "); the customer must override it")
    if by in HUMAN_CAPTURERS:
        # 1. explicit release by the customer, bound to the current terms
        if not valid_consent(t, ("release",)):
            raise RuleViolation("consent", "no recorded customer release for the current terms")
        return
    # automatic capture
    if t.state == State.DISPUTED:
        raise RuleViolation("dispute_open", "a dispute is open")
    if t.amendment is not None:
        raise RuleViolation("amendment_pending", "an amendment is waiting for the customer's consent")
    consent = valid_consent(t, ("auto_capture_on_verified", "amendment"))
    if not consent:
        raise RuleViolation("consent", "no recorded customer consent to automatic capture for the current terms")
    pend = t.pending
    if pend is None or pend.verdict_index is None or pend.verdict_index >= len(t.verdicts):
        raise RuleViolation("confidence", "an automatic capture must point at the verdict that justified it")
    rec = t.verdicts[pend.verdict_index]
    bar = max(min_confidence_floor, rec.min_confidence)
    if not pend.items and rec.verdict.confidence < bar:
        raise RuleViolation("confidence", f"confidence {rec.verdict.confidence:.2f} is below {bar:.2f}")
    if rec.action != "capture":
        raise RuleViolation("confidence", f"the verdict's decision was {rec.action}, not capture")
    if pend.items:  # itemized capture: every captured item must itself be met with confidence
        checks = {c.requirement_id: c for c in rec.verdict.checks}
        for rid in pend.items:
            c = checks.get(rid)
            conf = c.confidence if c is not None and c.confidence is not None else rec.verdict.confidence
            if c is None or c.result != "met" or conf < bar:
                raise RuleViolation("confidence", f"item {rid} was not verified as met with confidence >= {bar:.2f}")
            if t.item_status.get(rid) != "pending":
                raise RuleViolation("double_capture", f"item {rid} is already {t.item_status.get(rid)}")
    if rec.evidence_by not in consent.evidence_from:
        raise RuleViolation("consent", f"the customer did not agree to auto-capture on evidence from {rec.evidence_by}")
