"""The two demo runs, end to end, through the real job pipeline.

    python scripts/demo_jobs.py            # mock PayPal and AI unless credentials are set
    python scripts/demo_jobs.py --wait 600 # with Sandbox: approve each hold in a browser within 10 minutes

Run 1, good job: before and after photos, clean packet, the customer pays part: PayPal captures that part
(final_capture=false) and the rest of the hold is voided.
Run 2, reused photo: the after photo is a copy of run 1's after photo. The packet is flagged, the hard rule
refuses a capture without an override, the customer rejects, and the void lands.

Each run ends by verifying the job's hash-chained audit trail.
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.scenes import ROOMS, draw, jpeg  # noqa: E402
from janus import clock  # noqa: E402
from janus.cli import wait_authorize  # noqa: E402
from janus.service import Janus, OverrideRequired  # noqa: E402

# Says only what the generated photos show: the wall colour, a hole in the middle of the wall between the
# window and the door, and nothing else (no light switch, socket or skirting work).
DESC = ("Patch the hole in the middle of the {colour} {room} wall, between the window and the door. "
        "Sand it smooth. Paint the patch to match the {colour} wall.")


def say(msg: str) -> None:
    print(msg, flush=True)


def new_job(j: Janus, title: str, price: str, wait: int, room: str, colour: str):
    t = j.create_job(title, price, DESC.format(room=room, colour=colour))
    say(f"  job {t.id}: {title}, {t.amount} {t.currency}")
    for r in t.compiled.requirements:
        say(f"    checklist {r.id}: {r.statement}")
    j.consent(t.id, channel="demo")
    t = j.create_payment(t.id)
    if j.paypal.mode == "sandbox":
        say(f"  approve the hold with a Sandbox personal account:\n    {t.payment.approve_url}")
    t = wait_authorize(j, t.id, wait)
    say(f"  PayPal hold: authorization {t.payment.authorization_id} ({t.payment.authorization_status})")
    return t


def show_packet(t) -> None:
    v = t.last_verdict
    say(f"  packet: {t.state.value}, recommendation {v.action}, confidence {v.verdict.confidence:.2f}")
    for c in v.verdict.checks:
        conf = c.confidence if c.confidence is not None else v.verdict.confidence
        say(f"    {c.requirement_id} {c.result:<11} {conf:.2f}  {c.observation}")
    say(f"  flags: {', '.join(v.blocking_flags) or 'none'}")


def finish(j: Janus, t) -> None:
    ok, msg = j.audit.verify(t.id)
    p = j.paypal.get_authorization(t.id, t.payment.authorization_id).get("status")
    say(f"  outcome: {t.state.value} ({t.outcome.action if t.outcome else '-'}); PayPal authorization now {p}")
    for c in t.payment.captures:
        say(f"    capture {c.capture_id}: {c.amount} {'final' if c.final else 'partial'} (request {c.request_id})")
    say(f"  audit trail: {msg}")


def main(argv=None) -> int:
    a = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    a.add_argument("--wait", type=int, default=600, help="seconds to wait for Sandbox approval")
    a.add_argument("--partial", default="80.00")
    args = a.parse_args(argv)
    j = Janus.from_env()
    say(f"(paypal={j.paypal.mode}, ai={j.ai.mode}, storage={j.kv.name})")
    now = clock.utcnow()
    before1 = jpeg(draw(ROOMS["sage"], "hole", False, seed=1, layout=3), taken=now - timedelta(hours=2))
    after1 = jpeg(draw(ROOMS["sage"], "hole", True, seed=2, layout=3, shift=12, light=1.05), taken=now)

    say("\nRun 1: good job, partial capture, void of the rest")
    t = new_job(j, "Patch a hole in the living room wall", "120.00", args.wait, "living room", ROOMS["sage"].colour)
    j.submit_evidence(t.id, before1, by="seller", phase="before")
    j.submit_evidence(t.id, after1, by="seller", phase="after")
    t = j.verify(t.id)
    show_packet(t)
    say(f"  customer pays part: {args.partial}")
    t = j.partial(t.id, args.partial, channel="demo")
    finish(j, t)

    say("\nRun 2: reused photo, flagged, rule blocks, customer rejects, void")
    t = new_job(j, "Patch a hole in the hallway wall", "90.00", args.wait, "hallway", ROOMS["blue"].colour)
    before2 = jpeg(draw(ROOMS["blue"], "hole", False, seed=11, layout=13), taken=now - timedelta(hours=1))
    j.submit_evidence(t.id, before2, by="seller", phase="before")
    j.submit_evidence(t.id, after1, by="seller", phase="after")  # run 1's after photo, reused
    t = j.verify(t.id)
    show_packet(t)
    try:
        j.accept(t.id, channel="demo")
        say("  UNEXPECTED: accept went through without an override")
        return 1
    except OverrideRequired as e:
        say(f"  customer taps Accept without an override: refused ({e})")
    t = j.reject(t.id, "The after photo is from another job", channel="demo")
    finish(j, t)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
