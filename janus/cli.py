"""Script interface: python -m janus <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .ai import AIError
from .paypal import PayPalError
from .service import Janus, JanusError
from .state import TransitionError
from .store import NotFound, Threshold

SCENARIOS = {
    "match": ("photos/received_match.jpg", "Arrived today, here it is."),
    "mismatch": ("photos/received_mismatch.jpg", "This is what was in the box."),
    "unclear": ("photos/received_unclear.jpg", "Quick photo, sorry it's dark."),
}


def summary(t: Threshold) -> str:
    c, p = t.compiled, t.payment
    out = [f"{t.id}  [{t.state.value}]  {t.listing.title}  {t.amount} {t.currency}",
           f"  Condition: {t.condition}",
           f"  Compiled:  {c.summary}"]
    out += [f"    {r.id}{'*' if r.critical else ' '} {r.statement}" for r in c.requirements]
    if c.not_verifiable:
        out.append(f"  Not provable from a photo: {'; '.join(c.not_verifiable)}")
    if p.order_id:
        out.append(f"  PayPal: order={p.order_id} auth={p.authorization_id} ({p.authorization_status}) "
                   f"capture={p.capture_id} expires={p.expiration_time}")
    if p.last_error:
        out.append(f"  Last PayPal error: {p.last_error}")
    for v in t.verdicts:
        out.append(f"  Verdict on {','.join(v.evidence_ids)}: {v.verdict.verdict} "
                   f"(confidence {v.verdict.confidence:.2f}) -> {v.action.upper()}: {v.reason}")
        out += [f"    {k.requirement_id} {k.result:<11} {k.observation}" for k in v.verdict.checks]
        if v.verdict.next_step:
            out.append(f"    Next: {v.verdict.next_step}")
    if t.outcome and t.state.value in ("captured", "voided", "expired"):
        out.append(f"  OUTCOME: {t.outcome.action.upper()} by {t.outcome.by}: {t.outcome.reason}")
    return "\n".join(out)


def wait_authorize(j: Janus, tid: str, timeout: int) -> Threshold:
    deadline = time.time() + timeout
    while True:
        try:
            return j.authorize(tid)
        except JanusError as e:
            if time.time() > deadline or "not approved" not in str(e):
                raise
            print("  waiting for the buyer to approve on PayPal...", file=sys.stderr)
            time.sleep(5)


def create_from_args(j: Janus, a) -> Threshold:
    if a.listing:
        path = Path(a.listing)
        d = json.loads(path.read_text())
        photos = [path.parent / d["photo"]] if d.get("photo") else []
        return j.create(d["title"], a.price or d["price"], a.condition or d["condition"], d.get("description", ""),
                        d.get("currency", "USD"), photos)
    if not (a.title and a.price and a.condition):
        raise JanusError("give --listing FILE, or --title, --price and --condition")
    return j.create(a.title, a.price, a.condition, a.description or "", a.currency or "USD",
                    [a.photo] if a.photo else [])


def paypal_auth_status(j: Janus, t: Threshold) -> str:
    try:
        return j.paypal.get_authorization(t.id, t.payment.authorization_id).get("status") or "?"
    except PayPalError as e:
        return f"error ({e.issue or e.status})"


def demo(j: Janus, a) -> list[dict]:
    """The same listing, three deliveries: AI verdict -> policy -> real PayPal outcome."""
    listing = Path(a.listing)
    names = list(SCENARIOS) if a.scenario == "all" else [a.scenario]
    rows = []
    for name in names:
        photo, note = SCENARIOS[name]
        print(f"\n=================== scenario: {name} ===================")
        t = create_from_args(j, argparse.Namespace(listing=str(listing), price=None, condition=None))
        print(summary(t))
        t = j.create_payment(t.id)
        print(f"\n  Buyer link:          {j.buyer_link(t)}")
        print(f"  PayPal approval URL: {t.payment.approve_url}")
        if j.paypal.mode == "sandbox":
            print("  -> open the approval URL, log in as the SANDBOX PERSONAL (buyer) account, approve.")
        t = wait_authorize(j, t.id, a.wait)
        before = paypal_auth_status(j, t)
        print(f"  Authorized {t.amount} {t.currency}: {t.payment.authorization_id} (PayPal says {before})")
        print(f"\n  Buyer uploads {photo} ...")
        try:
            t = j.submit_and_verify(t.id, listing.parent / photo, note)
        except PayPalError as e:
            print(f"  PayPal error during settlement (retry with `janus settle {t.id}`): {e}")
            t = j.get(t.id)
        after = paypal_auth_status(j, t)
        print(summary(t))
        v = t.last_verdict
        rows.append({"scenario": name, "id": t.id, "verdict": v.verdict.verdict if v else "-",
                     "confidence": f"{v.verdict.confidence:.2f}" if v else "-",
                     "decision": v.action.upper() if v else "-", "state": t.state.value,
                     "before": before, "after": after, "capture": t.payment.capture_id or "-"})
    print("\n==================== before / after ====================")
    cols = ["scenario", "verdict", "confidence", "decision", "before", "after", "state", "capture"]
    print("  ".join(f"{c.upper():<12}" for c in cols))
    for r in rows:
        print("  ".join(f"{str(r[c]):<12}" for c in cols))
    print("BEFORE/AFTER = the authorization status PayPal itself reports, read back from the API.")
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="janus", description="Janus Threshold: describe what must be true; "
                                                          "Janus verifies it; PayPal moves the money.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create", help="seller states a condition; AI compiles it")
    s.add_argument("--listing", help="JSON file: title, description, price, currency, condition, photo")
    for f in ("title", "description", "price", "currency", "condition", "photo"):
        s.add_argument(f"--{f}")
    s = sub.add_parser("show"); s.add_argument("id"); s.add_argument("--json", action="store_true")
    sub.add_parser("list")
    s = sub.add_parser("pay", help="create the PayPal order (intent=AUTHORIZE), print the buyer link")
    s.add_argument("id")
    s = sub.add_parser("authorize", help="authorize after the buyer approved on PayPal")
    s.add_argument("id"); s.add_argument("--wait", type=int, default=0, help="poll up to N seconds")
    s = sub.add_parser("evidence", help="buyer submits a photo (add --verify to settle immediately)")
    s.add_argument("id"); s.add_argument("--photo"); s.add_argument("--note", default="")
    s.add_argument("--verify", action="store_true")
    s = sub.add_parser("verify", help="AI verdict -> capture / void / hold"); s.add_argument("id")
    s = sub.add_parser("release", help="buyer releases held money to the seller (capture)")
    s.add_argument("id"); s.add_argument("--note", default="")
    s = sub.add_parser("refund", help="seller releases the hold back to the buyer (void)")
    s.add_argument("id"); s.add_argument("--note", default="")
    s = sub.add_parser("settle", help="retry a capture/void that failed with a retryable error")
    s.add_argument("id")
    s = sub.add_parser("status", help="read the live authorization status from PayPal (detects expiry)")
    s.add_argument("id")
    sub.add_parser("sweep", help="status for every threshold that still holds funds")
    s = sub.add_parser("serve", help="run the web app + JSON API")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("demo", help="match vs mismatch vs unclear, end to end, with PayPal before/after")
    s.add_argument("--listing", default="examples/listing.json")
    s.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    s.add_argument("--wait", type=int, default=600, help="seconds to wait for sandbox buyer approval")

    a = p.parse_args(argv)
    j = Janus.from_env()
    print(f"(paypal={j.paypal.mode}, ai={j.ai.mode}, data={j.config.data_dir})", file=sys.stderr)
    try:
        if a.cmd == "create":
            t = create_from_args(j, a)
            print(summary(t))
            print(f"\nBuyer link:  {j.buyer_link(t)}\nSeller link: {j.seller_link(t)}")
        elif a.cmd == "show":
            t = j.get(a.id)
            print(t.model_dump_json(indent=2) if a.json else summary(t))
        elif a.cmd == "list":
            for t in j.store.all():
                print(f"{t.id}  {t.state.value:<18} {t.amount} {t.currency}  {t.listing.title}")
        elif a.cmd == "pay":
            t = j.create_payment(a.id)
            print(f"Buyer link: {j.buyer_link(t)}\nPayPal approval URL: {t.payment.approve_url}")
        elif a.cmd == "authorize":
            t = wait_authorize(j, a.id, a.wait) if a.wait else j.authorize(a.id)
            print(f"Authorized: {t.payment.authorization_id} ({t.payment.authorization_status}), "
                  f"expires {t.payment.expiration_time}")
        elif a.cmd == "evidence":
            t = j.submit_evidence(a.id, a.photo, a.note)
            print(summary(j.verify(a.id) if a.verify else t))
        elif a.cmd == "verify":
            print(summary(j.verify(a.id)))
        elif a.cmd == "release":
            print(summary(j.release(a.id, a.note)))
        elif a.cmd == "refund":
            print(summary(j.refund(a.id, a.note)))
        elif a.cmd == "settle":
            print(summary(j.retry_settlement(a.id)))
        elif a.cmd == "status":
            t = j.get(a.id)
            if t.payment.authorization_id:
                print(f"PayPal authorization {t.payment.authorization_id}: {paypal_auth_status(j, t)}")
            print(summary(j.refresh(a.id)))
        elif a.cmd == "sweep":
            for t in j.sweep():
                print(f"{t.id}  {t.state.value:<18} auth={t.payment.authorization_status} "
                      f"expires={t.payment.expiration_time}")
        elif a.cmd == "serve":
            from .server import serve
            serve(j, a.host, a.port)
        elif a.cmd == "demo":
            demo(j, a)
    except (JanusError, TransitionError, NotFound, PayPalError, AIError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0
