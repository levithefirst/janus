"""Logging: human-readable lines on stderr plus a JSON-lines audit log on disk.

Every PayPal call and AI decision goes through `log_event`, so `data/janus.log.jsonl`
is a complete, greppable record of what happened to each threshold.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("janus")
_audit_path: Path | None = None


def setup(data_dir: Path, level: int = logging.INFO) -> None:
    global _audit_path
    data_dir.mkdir(parents=True, exist_ok=True)
    _audit_path = data_dir / "janus.log.jsonl"
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        logger.addHandler(h)
    logger.setLevel(level)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log_event(category: str, action: str, ref: str | None = None, **data) -> dict:
    """category is 'paypal', 'ai' or 'state'. Returns the record (also stored on the job)."""
    record = {"ts": now(), "category": category, "action": action, "threshold_id": ref, **data}
    def short(v):
        return ",".join(map(str, v)) if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v) else v
    summary = " ".join(f"{k}={short(v)}" for k, v in data.items() if not isinstance(short(v), (dict, list)))
    logger.info("[%s] %s%s %s", category, action, f" id={ref}" if ref else "", summary)
    if _audit_path is not None:
        with _audit_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    return record
