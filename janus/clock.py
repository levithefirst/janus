"""The only source of time. Always timezone-aware UTC; tests can freeze or advance it."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

_frozen: Optional[datetime] = None


def utcnow() -> datetime:
    return _frozen if _frozen is not None else datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(s: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp as UTC. A timestamp with no zone is treated as UTC, never local time."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


@contextmanager
def frozen(at: datetime) -> Iterator["Travel"]:
    global _frozen
    prev, _frozen = _frozen, at.astimezone(timezone.utc)
    try:
        yield Travel()
    finally:
        _frozen = prev


class Travel:
    def advance(self, delta: timedelta) -> datetime:
        global _frozen
        _frozen = _frozen + delta
        return _frozen
