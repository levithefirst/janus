"""JSON-file store: one file per threshold at <data_dir>/thresholds/<id>/threshold.json."""
from __future__ import annotations

import json
import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, Optional

from pydantic import BaseModel, Field

from .logs import now
from .models import CompiledCondition, EvidenceItem, Listing, Verdict
from .state import TERMINAL, State

if TYPE_CHECKING:
    from .kv import Backend


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
    tenant: str = "public"       # tokens are bound to it; a tenant key only reaches its own thresholds
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
    """Thresholds as versioned documents, photos as blobs, on any storage backend (see kv.py)."""

    def __init__(self, backend: "Backend", cache_dir: Optional[Path] = None):
        self.kv = backend
        self.cache_dir = Path(cache_dir or "/tmp/janus-cache")

    @staticmethod
    def new_id() -> str:
        return "th_" + secrets.token_hex(8)  # an identifier, not a credential: access needs a token

    @staticmethod
    def _valid(tid: str) -> bool:
        return bool(tid) and "/" not in tid and ".." not in tid and "\x00" not in tid

    @staticmethod
    def _key(tid: str) -> str:
        return f"thresholds/{tid}/doc.json"

    @contextmanager
    def lock(self, tid: str) -> Iterator[None]:
        """Exclusive lock on one threshold, across threads, processes and (on Postgres) serverless instances."""
        if not self._valid(tid) or self.kv.get_doc(self._key(tid))[1] is None:
            raise NotFound(tid)
        with self.kv.lock(f"threshold:{tid}"):
            yield

    def save(self, t: Threshold) -> None:
        """Compare-and-set write: fails if the stored version moved since `t` was loaded, or if it is terminal."""
        disk_version, raw = self.kv.get_doc(self._key(t.id))
        if raw is not None:
            disk_state = State(json.loads(raw)["state"])
            if disk_state in TERMINAL:
                raise ImmutableError(f"{t.id} is {disk_state.value}; terminal thresholds cannot change")
        if disk_version != t.version:
            raise ConflictError(f"{t.id} changed in storage (version {disk_version}, expected {t.version})")
        old_version, old_updated = t.version, t.updated_at
        t.version, t.updated_at = old_version + 1, now()
        if not self.kv.cas_doc(self._key(t.id), old_version, t.model_dump_json(indent=2).encode()):
            t.version, t.updated_at = old_version, old_updated
            raise ConflictError(f"{t.id} changed in storage while saving (expected version {old_version})")

    def load(self, tid: str) -> Threshold:
        if not self._valid(tid):
            raise NotFound(tid)
        _, raw = self.kv.get_doc(self._key(tid))
        if raw is None:
            raise NotFound(tid)
        return Threshold.model_validate_json(raw)

    def all(self) -> list[Threshold]:
        ts = [Threshold.model_validate_json(raw) for raw in self.kv.docs("thresholds/")]
        return sorted(ts, key=lambda t: t.created_at)

    def find_by_order(self, order_id: str) -> Threshold:
        for t in self.all():
            if t.payment.order_id == order_id:
                return t
        raise NotFound(f"no threshold for PayPal order {order_id}")

    # --- photos ---

    def put_file(self, tid: str, rel: str, data: bytes) -> None:
        self.kv.put(f"thresholds/{tid}/{rel}", data)

    def get_file(self, tid: str, rel: str) -> Optional[bytes]:
        if not self._valid(tid):
            return None
        try:
            return self.kv.get(f"thresholds/{tid}/{rel}")
        except ValueError:
            return None

    def dir(self, tid: str) -> Path:
        """A local folder holding this threshold's photos as files (the AI reads them from here).

        On the file backend that is the storage itself. Elsewhere the blobs are copied into a local cache
        first; the cache is only a read copy and is safe to lose.
        """
        local = self.kv.local_dir
        if local is not None:
            return local / "thresholds" / tid
        d = self.cache_dir / tid
        for key in self.kv.keys(f"thresholds/{tid}/"):
            rel = key[len(f"thresholds/{tid}/"):]
            if rel == "doc.json":
                continue
            target = d / rel
            if not target.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.kv.get(key) or b"")
        return d
