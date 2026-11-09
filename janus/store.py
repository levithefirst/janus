"""JSON-file store: one file per threshold at <data_dir>/thresholds/<id>/threshold.json."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .logs import now
from .models import CompiledCondition, EvidenceItem, Listing, Verdict
from .state import State


class Payment(BaseModel):
    order_id: Optional[str] = None
    approve_url: Optional[str] = None
    authorization_id: Optional[str] = None
    authorization_status: Optional[str] = None   # last status PayPal reported
    authorized_at: Optional[str] = None
    expiration_time: Optional[str] = None
    capture_id: Optional[str] = None
    capture_status: Optional[str] = None
    voided: bool = False
    last_error: Optional[str] = None
    checked_at: Optional[str] = None


class VerdictRecord(BaseModel):
    at: str
    evidence_ids: list[str]
    verdict: Verdict
    action: str          # capture | void | hold
    reason: str


class Outcome(BaseModel):
    action: str          # capture | void | expired
    by: str              # janus (AI + policy) | buyer | seller | paypal
    reason: str
    at: str


class Threshold(BaseModel):
    id: str
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


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "thresholds"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        return "th_" + secrets.token_hex(5)

    @staticmethod
    def new_key() -> str:
        return secrets.token_urlsafe(12)

    def dir(self, tid: str) -> Path:
        return self.root / tid

    def save(self, t: Threshold) -> None:
        t.updated_at = now()
        d = self.dir(t.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "threshold.json.tmp"
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
