"""The threshold state machine. Every state change goes through `check`.

created -> payment_pending -> authorized -> evidence_submitted -> capturing -> captured
                                   |                |       \\-> voiding   -> voided
                                   |                \\-> held (more evidence, or a human decides)
                                   |                \\-> disputed (customer objects; humans resolve)
                                   |                \\-> packet_ready | manual_review (job mode: the customer
                                   |                     accepts, takes part, or rejects)
                                   \\-> expired (authorization ran out before settlement)
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    CREATED = "created"                        # condition compiled, waiting for the buyer
    PAYMENT_PENDING = "payment_pending"        # PayPal order created, buyer has the link
    AUTHORIZED = "authorized"                  # funds held by PayPal, nothing captured
    EVIDENCE_SUBMITTED = "evidence_submitted"  # buyer sent a photo, verification pending
    HELD = "held"                              # verdict uncertain: more evidence or a human decision
    PACKET_READY = "packet_ready"              # job mode: clean acceptance packet, the customer decides
    MANUAL_REVIEW = "manual_review"            # job mode: flags, low confidence or no AI packet; a person decides
    HOLD_LAPSED = "hold_lapsed"                # job mode: the hold expired or was released before the customer
                                               # decided; nothing is held; the customer may approve a new hold
    DISPUTED = "disputed"                      # customer disputes: no automatic settlement until resolved
    CAPTURING = "capturing"                    # capture in flight (or failed retryably: retry it)
    VOIDING = "voiding"                        # void in flight (or failed retryably: retry it)
    CAPTURED = "captured"                      # terminal: seller paid
    PARTIALLY_CAPTURED = "partially_captured"  # terminal (job mode): part captured, the rest of the hold voided
    VOIDED = "voided"                          # terminal: hold released, buyer not charged
    EXPIRED = "expired"                        # terminal: authorization expired, nothing moved


TERMINAL = {State.CAPTURED, State.VOIDED, State.EXPIRED, State.PARTIALLY_CAPTURED}
HOLDS_FUNDS = {State.AUTHORIZED, State.EVIDENCE_SUBMITTED, State.HELD, State.DISPUTED, State.CAPTURING,
               State.VOIDING, State.PACKET_READY, State.MANUAL_REVIEW}
DECIDABLE = {State.AUTHORIZED, State.EVIDENCE_SUBMITTED, State.HELD, State.DISPUTED, State.PACKET_READY,
             State.MANUAL_REVIEW}  # states in which the customer may accept, take part, or reject

ALLOWED: dict[State, set[State]] = {
    State.CREATED: {State.PAYMENT_PENDING},
    State.PAYMENT_PENDING: {State.AUTHORIZED},
    # The buyer may release (capture) or the seller refund (void) at any time while funds are held.
    State.AUTHORIZED: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.EXPIRED, State.DISPUTED,
                       State.HOLD_LAPSED},
    State.EVIDENCE_SUBMITTED: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.HELD, State.EXPIRED,
                               State.DISPUTED, State.PACKET_READY, State.MANUAL_REVIEW, State.HOLD_LAPSED},
    # Job mode: the AI's packet never moves money. The customer accepts (capture), takes part (capture part,
    # void the rest) or rejects (void); the provider may re-upload; a dispute or expiry can intervene.
    State.PACKET_READY: {State.CAPTURING, State.VOIDING, State.EVIDENCE_SUBMITTED, State.DISPUTED, State.EXPIRED,
                         State.HOLD_LAPSED},
    State.MANUAL_REVIEW: {State.CAPTURING, State.VOIDING, State.EVIDENCE_SUBMITTED, State.DISPUTED, State.EXPIRED,
                          State.HOLD_LAPSED},
    # Job mode: the hold lapsed. A new authorization resumes where the job was; or the job is closed.
    State.HOLD_LAPSED: {State.AUTHORIZED, State.EVIDENCE_SUBMITTED, State.PACKET_READY, State.MANUAL_REVIEW,
                        State.DISPUTED, State.HELD, State.VOIDED, State.EXPIRED},
    State.HELD: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.EXPIRED, State.DISPUTED,
                 State.HOLD_LAPSED},
    # A dispute stops automatic settlement. It ends when the customer withdraws it (held) or releases
    # (capture), the provider refunds (void), or the authorization nears expiry (void) or expires.
    State.DISPUTED: {State.HELD, State.CAPTURING, State.VOIDING, State.EXPIRED, State.HOLD_LAPSED},
    # A settlement can end somewhere other than planned when PayPal says the authorization
    # was already captured, voided or expired, or rejects the capture outright (-> held).
    # CAPTURING -> VOIDING: a partial capture is followed by a void of the rest.
    State.CAPTURING: {State.CAPTURED, State.VOIDED, State.EXPIRED, State.HELD, State.VOIDING,
                      State.PARTIALLY_CAPTURED, State.HOLD_LAPSED},
    State.VOIDING: {State.VOIDED, State.CAPTURED, State.EXPIRED, State.PARTIALLY_CAPTURED, State.HOLD_LAPSED},
    State.CAPTURED: set(),
    State.PARTIALLY_CAPTURED: set(),
    State.VOIDED: set(),
    State.EXPIRED: set(),
}


class TransitionError(Exception):
    pass


def check(current: State, target: State) -> None:
    if target not in ALLOWED[current]:
        allowed = ", ".join(s.value for s in ALLOWED[current]) or "none (terminal)"
        raise TransitionError(
            f"cannot go from '{current.value}' to '{target.value}'; allowed next: {allowed}"
        )
