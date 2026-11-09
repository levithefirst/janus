"""Logging: human-readable lines on stderr plus a JSON-lines audit log on disk.

Every PayPal call and AI decision goes through `log_event`, so `data/eid.log.jsonl`
is a complete, greppable record of what happened to each job.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("eid")
_audit_path: Path | None = None


def setup(data_dir: Path, level: int = logging.INFO) -> None:
    global _audit_path
    data_dir.mkdir(parents=True, exist_ok=True)
    _audit_path = data_dir / "eid.log.jsonl"
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        logger.addHandler(h)
    logger.setLevel(level)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(category: str, action: str, job_id: str | None = None, **data) -> dict:
    """category is 'paypal', 'ai' or 'state'. Returns the record (also stored on the job)."""
    record = {"ts": now(), "category": category, "action": action, "job_id": job_id, **data}
    def short(v):
        return ",".join(map(str, v)) if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v) else v
    summary = " ".join(f"{k}={short(v)}" for k, v in data.items() if not isinstance(short(v), (dict, list)))
    logger.info("[%s] %s%s %s", category, action, f" job={job_id}" if job_id else "", summary)
    if _audit_path is not None:
        with _audit_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    return record
