"""The job state machine. Every state change goes through `transition`."""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    CREATED = "created"                    # job card + checklist generated
    PAYMENT_PENDING = "payment_pending"    # PayPal order created, customer link issued
    AUTHORIZED = "authorized"              # funds authorized (held), not captured
    EVIDENCE_SUBMITTED = "evidence_submitted"
    PACKET_READY = "packet_ready"          # gap report + acceptance packet generated
    APPROVED = "approved"                  # customer approved; capture in progress
    DISPUTED = "disputed"                  # customer disputed / requested fix; void in progress
    CAPTURED = "captured"                  # terminal: provider paid
    VOIDED = "voided"                      # terminal: authorization released


ALLOWED: dict[State, set[State]] = {
    State.CREATED: {State.PAYMENT_PENDING},
    State.PAYMENT_PENDING: {State.AUTHORIZED},
    State.AUTHORIZED: {State.EVIDENCE_SUBMITTED},
    # Provider may add more evidence after seeing a gap report, before the customer decides.
    State.EVIDENCE_SUBMITTED: {State.EVIDENCE_SUBMITTED, State.PACKET_READY},
    State.PACKET_READY: {State.EVIDENCE_SUBMITTED, State.APPROVED, State.DISPUTED},
    State.APPROVED: {State.CAPTURED},
    State.DISPUTED: {State.VOIDED},
    State.CAPTURED: set(),
    State.VOIDED: set(),
}


class TransitionError(Exception):
    pass


def check(current: State, target: State) -> None:
    if target not in ALLOWED[current]:
        allowed = ", ".join(s.value for s in ALLOWED[current]) or "none (terminal)"
        raise TransitionError(
            f"cannot go from '{current.value}' to '{target.value}'; allowed next: {allowed}"
        )
