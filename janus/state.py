"""The threshold state machine. Every state change goes through `check`.

created -> payment_pending -> authorized -> evidence_submitted -> capturing -> captured
                                   |                |       \\-> voiding   -> voided
                                   |                \\-> held (more evidence, or a human decides)
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
    CAPTURING = "capturing"                    # capture in flight (or failed retryably: retry it)
    VOIDING = "voiding"                        # void in flight (or failed retryably: retry it)
    CAPTURED = "captured"                      # terminal: seller paid
    VOIDED = "voided"                          # terminal: hold released, buyer not charged
    EXPIRED = "expired"                        # terminal: authorization expired, nothing moved


TERMINAL = {State.CAPTURED, State.VOIDED, State.EXPIRED}
HOLDS_FUNDS = {State.AUTHORIZED, State.EVIDENCE_SUBMITTED, State.HELD, State.CAPTURING, State.VOIDING}

ALLOWED: dict[State, set[State]] = {
    State.CREATED: {State.PAYMENT_PENDING},
    State.PAYMENT_PENDING: {State.AUTHORIZED},
    # The buyer may release (capture) or the seller refund (void) at any time while funds are held.
    State.AUTHORIZED: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.EXPIRED},
    State.EVIDENCE_SUBMITTED: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.HELD, State.EXPIRED},
    State.HELD: {State.EVIDENCE_SUBMITTED, State.CAPTURING, State.VOIDING, State.EXPIRED},
    # A settlement can end somewhere other than planned when PayPal says the authorization
    # was already captured, voided or expired, or rejects the capture outright (-> held).
    State.CAPTURING: {State.CAPTURED, State.VOIDED, State.EXPIRED, State.HELD},
    State.VOIDING: {State.VOIDED, State.CAPTURED, State.EXPIRED},
    State.CAPTURED: set(),
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
