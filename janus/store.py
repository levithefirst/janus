"""JSON-file store: one file per threshold at <data_dir>/thresholds/<id>/threshold.json."""
from __future__ import annotations

import fcntl
import os
import secrets
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field

from .logs import now
from .models import CompiledCondition, EvidenceItem, Listing, Verdict
from .state import TERMINAL, State


class Consent(BaseModel):
    """An explicit customer decision, bound to the exact terms it was given for."""
    kind: str            # auto_capture_on_verified | release | amendment
    by: str              # always the customer (buyer); recorded for the audit trail
    at: str
    terms_hash: str
    amount: str
    channel: str         # web | api | cli
    evidence_from: list[str] = Field(default_factory=lambda: ["buyer"])  # whose evidence may auto-capture


class Capture(BaseModel):
    capture_id: str
    amount: str
    request_id: str
    final: bool
    items: list[str] = Field(default_factory=list)
    at: str


class Pending(BaseModel):
    """A settlement written to disk BEFORE PayPal is called, so a crash can replay it exactly."""
    action: str          # capture | void
    by: str
    reason: str
    request_id: str
    amount: Optional[str] = None
    final: bool = True
    items: list[str] = Field(default_factory=list)
    verdict_index: Optional[int] = None   # which verdict justified an automatic capture
    at: str


class Amendment(BaseModel):
    """A change of scope proposed by the provider. Nothing changes until the customer accepts it."""
    condition: Optional[str] = None
    price: Optional[str] = None
    reason: str = ""
    proposed_by: str = "seller"
    at: str


class Payment(BaseModel):
    order_id: Optional[str] = None
    authorized_amount: Optional[str] = None
    approve_url: Optional[str] = None
    authorization_id: Optional[str] = None
    authorization_status: Optional[str] = None   # last status PayPal reported
    authorized_at: Optional[str] = None
    expiration_time: Optional[str] = None
    capture_id: Optional[str] = None
    capture_status: Optional[str] = None
    voided: bool = False
    captures: list[Capture] = Field(default_factory=list)
    original_authorized_at: Optional[str] = None   # the 29-day clock starts here, reauthorize does not reset it
    reauthorize_count: int = 0
    previous_authorization_ids: list[str] = Field(default_factory=list)
    honor_warned: bool = False
    needs_new_authorization: bool = False
    last_error: Optional[str] = None
    checked_at: Optional[str] = None


class VerdictRecord(BaseModel):
    at: str
    evidence_ids: list[str]
    verdict: Verdict
    action: str          # capture | void | hold
    reason: str
    min_confidence: float = 0.85          # the bar this verdict had to clear (base + evidence trust penalty)
    evidence_by: str = "buyer"            # who submitted the evidence the verdict is about
    source: str = "ai"                    # ai | quality_gate | integrity
    trust_flags: list[str] = Field(default_factory=list)


class Outcome(BaseModel):
    action: str          # capture | void | expired
    by: str              # janus (AI + policy) | buyer | seller | paypal
    reason: str
    at: str


class Threshold(BaseModel):
    id: str
    version: int = 0             # compare-and-set: bumped on every save
    seller_key: str
    state: State
    created_at: str
    updated_at: str
    listing: Listing
    condition: str
    compiled: CompiledCondition
    payment: Payment = Field(default_factory=Payment)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    verdicts: list[VerdictRecord] = Field(default_factory=list)
    outcome: Optional[Outcome] = None
    consents: list[Consent] = Field(default_factory=list)
    pending: Optional[Pending] = None
    ever_voided_or_expired: bool = False
    delivery_location: Optional[list[float]] = None   # optional [lat, lon] for the GPS check
    allocations: dict[str, str] = Field(default_factory=dict)  # requirement id -> amount (itemized capture)
    item_status: dict[str, str] = Field(default_factory=dict)  # requirement id -> pending|captured|released
    amendment: Optional[Amendment] = None
    events: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def amount(self) -> str:
        return self.listing.price

    @property
    def currency(self) -> str:
        return self.listing.currency

    @property
    def last_verdict(self) -> Optional[VerdictRecord]:
        return self.verdicts[-1] if self.verdicts else None


class NotFound(Exception):
    pass


class ConflictError(Exception):
    """Someone else saved this threshold since it was loaded (compare-and-set failed)."""


class ImmutableError(Exception):
    """The threshold is in a terminal state; nothing may change it."""


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "thresholds"
        self.root.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._tlocks: dict[str, threading.Lock] = {}

    @staticmethod
    def new_id() -> str:
        return "th_" + secrets.token_hex(5)

    @staticmethod
    def new_key() -> str:
        return secrets.token_urlsafe(12)

    def dir(self, tid: str) -> Path:
        return self.root / tid

    @contextmanager
    def lock(self, tid: str) -> Iterator[None]:
        """Exclusive lock on one threshold, across threads and processes (CLI, server, scheduler)."""
        d = self.dir(tid)
        if not d.is_dir() or "/" in tid or ".." in tid:
            raise NotFound(tid)
        with self._guard:
            tl = self._tlocks.setdefault(tid, threading.Lock())
        with tl, (d / ".lock").open("a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _disk_version(self, tid: str) -> tuple[int, Optional[State]]:
        p = self.dir(tid) / "threshold.json"
        if not p.is_file():
            return 0, None
        raw = Threshold.model_validate_json(p.read_text())
        return raw.version, raw.state

    def save(self, t: Threshold) -> None:
        """Compare-and-set write: fails if the file changed since `t` was loaded, or if it is terminal."""
        disk_version, disk_state = self._disk_version(t.id)
        if disk_state in TERMINAL:
            raise ImmutableError(f"{t.id} is {disk_state.value}; terminal thresholds cannot change")
        if disk_version != t.version:
            raise ConflictError(f"{t.id} changed on disk (version {disk_version}, expected {t.version})")
        t.version += 1
        t.updated_at = now()
        d = self.dir(t.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f"threshold.json.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(t.model_dump_json(indent=2))
        os.replace(tmp, d / "threshold.json")

    def load(self, tid: str) -> Threshold:
        p = self.dir(tid) / "threshold.json"
        if not p.is_file() or "/" in tid or ".." in tid:
            raise NotFound(tid)
        return Threshold.model_validate_json(p.read_text())

    def all(self) -> list[Threshold]:
        ts = [Threshold.model_validate_json(p.read_text()) for p in self.root.glob("*/threshold.json")]
        return sorted(ts, key=lambda t: t.created_at)

    def find_by_order(self, order_id: str) -> Threshold:
        for t in self.all():
            if t.payment.order_id == order_id:
                return t
        raise NotFound(f"no threshold for PayPal order {order_id}")
