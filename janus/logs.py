"""Operational logs: structured records on stderr plus a JSON-lines file on disk, with PII redacted.

Every PayPal call and AI decision goes through `log_event`, so `data/janus.log.jsonl` is a
greppable record of what happened. Decisions also go to the append-only audit log (audit.py).

Redaction runs on every record before it is written anywhere: values under sensitive keys are
replaced, and emails, phone numbers, bearer tokens and URL credentials are masked inside strings.
Set JANUS_LOG_FORMAT=json for one JSON object per line on stderr.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("janus")
_log_path: Path | None = None

SENSITIVE_KEYS = {"note", "notes", "email", "payer", "payer_email", "name", "given_name", "surname", "address",
                  "phone", "token", "key", "seller_key", "buyer_token", "secret", "client_secret", "password",
                  "authorization_header", "photo_b64", "gps", "lat", "lon", "latitude", "longitude",
                  "access_token", "sig", "transmission_sig", "cert_url"}
_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [redacted]"),
    (re.compile(r"(?i)([?&](?:k|b|t|token|key|sig|access_token)=)[^&\s\"']+"), r"\1[redacted]"),
    (re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])"), "[phone]"),
]


def redact(value: Any, key: str | None = None) -> Any:
    if key is not None and key.lower() in SENSITIVE_KEYS:
        return "[redacted]" if value not in (None, "", [], {}) else value
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pat, repl in _PATTERNS:
            value = pat.sub(repl, value)
    return value


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(getattr(record, "janus", {"msg": record.getMessage()}), default=str)


def setup(data_dir: Path, level: int = logging.INFO) -> None:
    global _log_path
    data_dir.mkdir(parents=True, exist_ok=True)
    _log_path = data_dir / "janus.log.jsonl"
    if not logger.handlers:
        h = logging.StreamHandler(sys.stderr)
        if os.environ.get("JANUS_LOG_FORMAT") == "json":
            h.setFormatter(_JsonFormatter())
        else:
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S"))
        logger.addHandler(h)
    logger.setLevel(level)


def now() -> str:
    from .clock import iso
    return iso()


def log_event(category: str, action: str, ref: str | None = None, **data) -> dict:
    """category is 'paypal', 'ai', 'state', 'security' or 'scheduler'. Returns the redacted record."""
    record = redact({"ts": now(), "category": category, "action": action, "threshold_id": ref, **data})

    def short(v):
        return ",".join(map(str, v)) if isinstance(v, list) and all(not isinstance(x, (dict, list)) for x in v) else v
    summary = " ".join(f"{k}={short(v)}" for k, v in record.items()
                       if k not in ("ts", "category", "action", "threshold_id") and not isinstance(short(v), (dict, list)))
    logger.info("[%s] %s%s %s", category, action, f" id={ref}" if ref else "", summary, extra={"janus": record})
    if _log_path is not None:
        with _log_path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    return record
