"""Measure two PayPal Sandbox behaviours the docs leave open. Records observations; draws no conclusions.

1. Reauthorization limits: how many times, and from which day, can one authorization be reauthorized?
   The docs conflict ("multiple re-authorizations" vs "only once from days four to 29"; see constants.py).

       python scripts/measure_sandbox.py reauth-start     # creates an order and prints the approval URL
       (approve it in a browser with a Sandbox personal account)
       python scripts/measure_sandbox.py reauth-authorize # authorizes the approved order
       python scripts/measure_sandbox.py reauth-try       # run once a day from day 1 to day 29

   Each reauth-try records the authorization's age, PayPal's status code and issue, and whether a new
   authorization id came back. After a success it tries again at once, to probe the count limit.

2. PayPal-Request-Id retention: for how long does PayPal replay the original response for a repeated key?

       python scripts/measure_sandbox.py request-id-start  # creates an order with a fixed request id
       python scripts/measure_sandbox.py request-id-check  # run at, say, 1 h, 6 h, 24 h, 72 h, 7 d, 30 d

   Creating orders is harmless in the Sandbox. A check that returns the original order id means the key was
   still retained at that age; a new order id means it was not.

Every observation is appended to measurements.jsonl (path: --log). Nothing here changes Janus's behaviour.
TODO(sandbox): run both protocols live and record the observations in docs/HARD_QUESTIONS.md; until then
constants.MAX_REAUTHORIZATIONS stays at 1 and no request-id retention window is assumed.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from janus.paypal import PayPalError, PayPalSandbox  # noqa: E402


def now() -> datetime:
    return datetime.now(timezone.utc)


class Recorder:
    def __init__(self, log: Path, state: Path):
        self.log, self.state_path = log, state

    def state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.is_file() else {}

    def save(self, st: dict) -> None:
        self.state_path.write_text(json.dumps(st, indent=2))

    def record(self, **obs) -> dict:
        obs = {"at": now().isoformat(timespec="seconds"), **obs}
        with self.log.open("a") as f:
            f.write(json.dumps(obs) + "\n")
        print(json.dumps(obs))
        return obs


def _err(e: PayPalError) -> dict:
    return {"ok": False, "status": e.status, "issue": e.issue, "debug_id": e.debug_id}


def _age_hours(iso: str) -> float:
    return round((now() - datetime.fromisoformat(iso)).total_seconds() / 3600, 2)


# ---------------- 1. reauthorization limits ----------------

def reauth_start(pp, rec: Recorder) -> dict:
    ref = f"measure-reauth-{uuid.uuid4().hex[:8]}"
    o = pp.create_order(ref, "10.00", "USD", "Janus reauthorization measurement", "https://example.com/return",
                        "https://example.com/cancel", request_id=f"{ref}-create")
    rec.save({"reauth": {"ref": ref, "order_id": o["order_id"]}} | {k: v for k, v in rec.state().items() if k != "reauth"})
    print(f"Approve with a Sandbox personal account, then run reauth-authorize:\n  {o['approve_url']}")
    return rec.record(protocol="reauth", step="start", order_id=o["order_id"])


def reauth_authorize(pp, rec: Recorder) -> dict:
    st = rec.state()
    r = st["reauth"]
    a = pp.authorize_order(r["ref"], r["order_id"], request_id=f"{r['ref']}-authorize")
    r.update(authorization_id=a["authorization_id"], original_authorization_id=a["authorization_id"],
             authorized_at=now().isoformat(timespec="seconds"), attempts=0, successes=0)
    rec.save(st)
    return rec.record(protocol="reauth", step="authorize", authorization_id=a["authorization_id"],
                      expiration_time=a.get("expiration_time"))


def reauth_try(pp, rec: Recorder, again_after_success: bool = True) -> list[dict]:
    st = rec.state()
    r = st["reauth"]
    out = []
    while True:
        r["attempts"] += 1
        key = f"{r['ref']}-reauthorize-{r['attempts']}"
        base = {"protocol": "reauth", "step": "try", "attempt": r["attempts"], "successes_before": r["successes"],
                "age_hours": _age_hours(r["authorized_at"]), "authorization_id": r["authorization_id"]}
        try:
            res = pp.reauthorize(r["ref"], r["authorization_id"], key)
        except PayPalError as e:
            out.append(rec.record(**base, **_err(e)))
            rec.save(st)
            return out
        r["successes"] += 1
        r["authorization_id"] = res["authorization_id"]
        out.append(rec.record(**base, ok=True, new_authorization_id=res["authorization_id"],
                              expiration_time=res.get("expiration_time")))
        rec.save(st)
        if not again_after_success:
            return out


# ---------------- 2. request-id retention ----------------

def request_id_start(pp, rec: Recorder) -> dict:
    key = f"measure-rid-{uuid.uuid4().hex[:12]}"
    o = pp.create_order(key, "1.00", "USD", "Janus request-id retention measurement", "https://example.com/return",
                        "https://example.com/cancel", request_id=key)
    st = rec.state()
    st["request_id"] = {"key": key, "order_id": o["order_id"], "sent_at": now().isoformat(timespec="seconds")}
    rec.save(st)
    return rec.record(protocol="request_id", step="start", key=key, order_id=o["order_id"])


def request_id_check(pp, rec: Recorder) -> dict:
    r = rec.state()["request_id"]
    o = pp.create_order(r["key"], "1.00", "USD", "Janus request-id retention measurement", "https://example.com/return",
                        "https://example.com/cancel", request_id=r["key"])
    return rec.record(protocol="request_id", step="check", key=r["key"], age_hours=_age_hours(r["sent_at"]),
                      original_order_id=r["order_id"], returned_order_id=o["order_id"],
                      replayed=o["order_id"] == r["order_id"])


COMMANDS = {"reauth-start": reauth_start, "reauth-authorize": reauth_authorize, "reauth-try": reauth_try,
            "request-id-start": request_id_start, "request-id-check": request_id_check}


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Record PayPal Sandbox observations. Draws no conclusions.")
    p.add_argument("command", choices=sorted(COMMANDS))
    p.add_argument("--log", default="measurements.jsonl")
    p.add_argument("--state", default="measurements.state.json")
    a = p.parse_args(argv)
    from janus.config import Config
    c = Config.from_env()
    if not c.paypal_client_id or not c.paypal_client_secret:
        print("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET (Sandbox) are required.", file=sys.stderr)
        return 2
    from janus.logs import setup
    setup(Path(c.data_dir))
    COMMANDS[a.command](PayPalSandbox(c.paypal_client_id, c.paypal_client_secret), Recorder(Path(a.log), Path(a.state)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
