from datetime import datetime, timedelta, timezone

from janus import clock
from janus.constants import AUTH_EXPIRY_DAYS, HONOR_DAYS


def test_constants():
    assert (HONOR_DAYS, AUTH_EXPIRY_DAYS) == (3, 29)


def test_parse_is_always_utc():
    assert clock.parse("2026-11-09T10:00:00").tzinfo == timezone.utc           # naive -> UTC, never local
    assert clock.parse("2026-11-09T10:00:00Z") == clock.parse("2026-11-09T05:00:00-05:00")
    assert clock.parse("nonsense") is None and clock.parse(None) is None


def test_frozen_clock_advances():
    start = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    with clock.frozen(start) as t:
        assert clock.utcnow() == start and clock.iso().endswith("+00:00")
        t.advance(timedelta(days=1))
        assert clock.utcnow() == start + timedelta(days=1)
    assert clock.utcnow() != start
