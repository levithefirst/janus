"""Background scheduler: crash recovery plus the authorization-expiry timeline.

    day 0     authorized
    day 2.5   warn: honor period ends in 12 hours           (WARN_BEFORE_HONOR)
    day 3     reauthorize, or ask the customer for a new authorization
    day 27    void, so funds are released before PayPal expires the authorization (VOID_BEFORE_EXPIRY, 48 h:
              a once-a-day cron still lands inside the window)
    day 29    PayPal expiry (AUTH_EXPIRY_DAYS)

Run it with `python -m janus tick` from cron, or it runs every JANUS_TICK_SECONDS inside `serve`.
It never captures: only a verified condition (with consent) or the customer can.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .logs import log_event
from .paypal import PayPalError
from .state import HOLDS_FUNDS, State

if TYPE_CHECKING:
    from .service import Janus


def tick(j: "Janus") -> list[dict]:
    actions = [{"threshold_id": t.id, "action": f"recovered:{t.state.value}"} for t in j.recover()]
    for t in j.store.all():
        if t.state in HOLDS_FUNDS and t.state not in (State.CAPTURING, State.VOIDING):
            try:
                actions += [{"threshold_id": t.id, "action": a} for a in j.expiry_tick(t.id)]
            except PayPalError as e:  # retried next tick
                actions.append({"threshold_id": t.id, "action": f"error:{e.issue or e.status}"})
    for a in actions:
        log_event("scheduler", a["action"], a["threshold_id"])
    return actions


def start_background(j: "Janus", every_seconds: float) -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.wait(every_seconds):
            try:
                tick(j)
            except Exception as e:  # never let the scheduler thread die
                log_event("scheduler", "tick_failed", error=str(e))
    threading.Thread(target=loop, name="janus-scheduler", daemon=True).start()
    return stop
