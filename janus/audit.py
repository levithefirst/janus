"""Append-only, hash-chained audit log of every decision and its reason.

One JSON object per line in <data_dir>/audit.jsonl. Each entry carries the SHA-256 of the
previous entry, so editing, deleting or reordering any line breaks the chain and `verify()`
reports where. The file is only ever opened in append mode, under an exclusive lock, so
concurrent writers (server threads, CLI, scheduler) cannot interleave or fork the chain.

What counts as a decision: condition compiled, consent given, AI verdict, policy action,
capture/void/refund/reauthorize attempted and its result, rule blocks, holds, disputes,
amendments, scheduler actions, webhook effects.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from .clock import iso
from .logs import redact

GENESIS = "0" * 64


def _digest(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "audit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    @staticmethod
    def _last(f) -> tuple[int, str]:
        f.seek(0)
        seq, prev = 0, GENESIS
        for line in f:
            if line.strip():
                e = json.loads(line)
                seq, prev = e["seq"], e["hash"]
        return seq, prev

    def append(self, kind: str, threshold_id: Optional[str], actor: str, reason: str, **data: Any) -> dict:
        with self.path.open("a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                seq, prev = self._last(f)
                entry = {"seq": seq + 1, "ts": iso(), "kind": kind, "threshold_id": threshold_id,
                         "actor": actor, "reason": reason, "data": redact(data), "prev": prev}
                entry["hash"] = _digest(entry)
                f.seek(0, 2)
                f.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
                f.flush()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        return entry

    def entries(self, threshold_id: Optional[str] = None) -> list[dict]:
        out = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        return [e for e in out if threshold_id is None or e["threshold_id"] == threshold_id]

    def verify(self) -> tuple[bool, str]:
        prev, seq = GENESIS, 0
        for n, line in enumerate(self.path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("prev") != prev or e.get("seq") != seq + 1 or _digest(e) != e.get("hash"):
                return False, f"chain broken at line {n} (seq {e.get('seq')})"
            prev, seq = e["hash"], e["seq"]
        return True, f"{seq} entries, chain intact"
