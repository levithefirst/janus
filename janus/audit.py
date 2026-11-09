"""Append-only, hash-chained audit logs: one global chain, and one chain per job.

Every entry carries `prev`, the SHA-256 of the previous entry in the same chain, and its own `hash` over
everything else. Editing, deleting or reordering any entry breaks the chain, and `verify()` reports where.
Appends go through the storage backend's locked append, so concurrent writers (server threads, CLI,
scheduler, other serverless instances on Postgres) cannot interleave or fork a chain.

The per-job chain records, for that job: every PayPal call, every rule check, every AI request and output,
every state change and every user action (consent, accept, partial, reject, override, uploads).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Union

from .clock import iso
from .kv import Backend, FileBackend
from .logs import redact

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _chain(prev_entry: Optional[dict], body: dict) -> dict:
    entry = dict(body, seq=(prev_entry["seq"] + 1) if prev_entry else 1,
                 prev=prev_entry["hash"] if prev_entry else GENESIS)
    entry["hash"] = _digest(entry)
    return entry


def verify_entries(entries: list[dict]) -> tuple[bool, str]:
    prev, seq = GENESIS, 0
    for n, e in enumerate(entries, 1):
        if e.get("prev") != prev or e.get("seq") != seq + 1 or _digest(e) != e.get("hash"):
            return False, f"chain broken at entry {n} (seq {e.get('seq')})"
        prev, seq = e["hash"], e["seq"]
    return True, f"{seq} entries, chain intact"


class AuditLog:
    GLOBAL = "audit"

    def __init__(self, store: Union[Path, Backend]):
        self.kv = store if isinstance(store, Backend) else FileBackend(Path(store))

    @property
    def path(self) -> Path:
        """The global chain's file, on the file backend."""
        return self.kv.local_dir / f"{self.GLOBAL}.jsonl"

    @staticmethod
    def job_stream(threshold_id: str) -> str:
        return f"audit/{threshold_id}"

    def append(self, kind: str, threshold_id: Optional[str], actor: str, reason: str, **data: Any) -> dict:
        body = {"ts": iso(), "kind": kind, "threshold_id": threshold_id, "actor": actor, "reason": reason,
                "data": redact(data)}
        entry = self.kv.append(self.GLOBAL, lambda last: _chain(last, body))
        if threshold_id:
            self.kv.append(self.job_stream(threshold_id),
                           lambda last: _chain(last, dict(body, global_seq=entry["seq"])))
        return entry

    def entries(self, threshold_id: Optional[str] = None) -> list[dict]:
        return self.kv.stream(self.job_stream(threshold_id) if threshold_id else self.GLOBAL)

    def verify(self, threshold_id: Optional[str] = None) -> tuple[bool, str]:
        return verify_entries(self.entries(threshold_id))

    def head(self, threshold_id: Optional[str] = None) -> Optional[str]:
        e = self.entries(threshold_id)
        return e[-1]["hash"] if e else None
