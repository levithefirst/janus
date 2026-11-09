"""Script interface: python -m eid <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .ai import AIError
from .paypal import PayPalError
from .service import Eid, EidError
from .state import TransitionError
from .store import Job, JobNotFound


def summary(job: Job) -> str:
    c = job.card
    out = [f"{job.id}  [{job.state.value}]  {c.title}  {job.amount} {job.currency}"]
    out.append("  Checklist:")
    out += [f"    {i.id}  {i.description}" for i in c.checklist]
    out.append("  Required evidence:")
    out += [f"    {e.id}  ({e.kind}) {e.description}  -> {', '.join(e.checklist_item_ids)}" for e in c.required_evidence]
    p = job.payment
    if p.order_id:
        out.append(f"  PayPal: order={p.order_id} auth={p.authorization_id} capture={p.capture_id} voided={p.voided}")
    if job.evidence:
        out.append("  Evidence: " + ", ".join(f"{e.id}->{e.requirement_id or '?'}({e.kind})" for e in job.evidence))
    if job.gap_report:
        out.append(f"  AI recommendation: {job.gap_report.recommended_action} ({job.gap_report.confidence})")
    return "\n".join(out)


def wait_authorize(eid: Eid, job_id: str, timeout: int) -> Job:
    deadline = time.time() + timeout
    while True:
        try:
            return eid.authorize(job_id)
        except EidError as e:
            if time.time() > deadline or "not approved" not in str(e):
                raise
            print("  waiting for the customer to approve on PayPal...", file=sys.stderr)
            time.sleep(5)


def evidence_from_args(a) -> tuple[list[dict], Path]:
    if a.json:
        path = Path(a.json)
        data = json.loads(path.read_text())
        return (data["items"] if isinstance(data, dict) else data), path.parent
    if not (a.note or a.file):
        raise EidError("give --json FILE, or --note/--file for a single item")
    return [{"requirement_id": a.req, "kind": a.kind, "note": a.note or "", "file": a.file}], Path(".")


def demo(eid: Eid, a) -> Job:
    """Run the whole loop in one go."""
    def step(n, msg):
        print(f"\n=== {n}. {msg} ===")

    step(1, "Create job from text")
    text = Path(a.text_file).read_text()
    print(text.strip())
    step(2, "AI job card + checklist + required evidence")
    job = eid.create_job(text, a.amount)
    print(summary(job))
    step(3, "PayPal order (intent=AUTHORIZE) and customer authorization")
    job = eid.create_payment(job.id)
    print(f"  Customer link: {eid.customer_link(job)}")
    print(f"  PayPal approval URL: {job.payment.approve_url}")
    if eid.paypal.mode == "sandbox":
        print("  Open the approval URL, log in with a SANDBOX PERSONAL account, and approve.")
    job = wait_authorize(eid, job.id, a.wait)
    print(f"  Authorized: {job.payment.authorization_id}")
    step(4, "Provider submits evidence")
    items, base = evidence_from_args(argparse.Namespace(json=a.evidence_file, note=None, file=None))
    job = eid.submit_evidence(job.id, items, base)
    print("  " + ", ".join(f"{e.id}: {e.note[:50]}" for e in job.evidence))
    step(5, "AI gap report + acceptance packet")
    job = eid.generate_packet(job.id)
    print(job.packet_markdown)
    outcome = a.outcome
    if outcome == "auto":
        outcome = "approve" if job.gap_report.recommended_action == "approve" else "dispute"
    step(6, f"Customer decision: {outcome}")
    if outcome == "approve":
        job = eid.approve(job.id, "Looks good")
        print(f"  CAPTURED: capture_id={job.payment.capture_id} status={job.payment.capture_status}")
    else:
        gaps = "; ".join(job.gap_report.gaps)[:300]
        reason = f"Evidence incomplete: {gaps}" if gaps else "Customer is not satisfied with the work"
        job = eid.dispute(job.id, reason, "fix" if outcome == "fix" else "dispute")
        print(f"  VOIDED: authorization {job.payment.authorization_id} released")
    print(f"\nFinal state: {job.state.value}. Job file: {eid.store.job_dir(job.id) / 'job.json'}")
    return job


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="eid", description="Eid core loop")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("create", help="1+2: create a job from text and generate the job card")
    s.add_argument("text", nargs="?", help="job text (or use --file / stdin)")
    s.add_argument("--file")
    s.add_argument("--amount")
    s.add_argument("--currency")

    s = sub.add_parser("show"); s.add_argument("job"); s.add_argument("--json", action="store_true")
    sub.add_parser("list")
    s = sub.add_parser("pay", help="3a: create PayPal order, print customer link"); s.add_argument("job")
    s = sub.add_parser("authorize", help="3b: authorize after customer approval")
    s.add_argument("job"); s.add_argument("--wait", type=int, default=0, help="poll up to N seconds")
    s = sub.add_parser("evidence", help="4: submit evidence")
    s.add_argument("job"); s.add_argument("--json", help="file with {\"items\": [...]}")
    s.add_argument("--req"); s.add_argument("--kind", default="note"); s.add_argument("--note"); s.add_argument("--file")
    s = sub.add_parser("packet", help="5: AI gap report + acceptance packet"); s.add_argument("job")
    s = sub.add_parser("approve", help="6: approve -> capture"); s.add_argument("job"); s.add_argument("--note", default="")
    s = sub.add_parser("dispute", help="6: dispute or request fix -> void")
    s.add_argument("job"); s.add_argument("--reason", required=True); s.add_argument("--fix", action="store_true")
    s = sub.add_parser("serve", help="run the HTTP API")
    s.add_argument("--host", default="127.0.0.1"); s.add_argument("--port", type=int, default=8000)
    s = sub.add_parser("demo", help="run the full loop end to end")
    s.add_argument("--text-file", default="examples/job.txt")
    s.add_argument("--evidence-file", default="examples/evidence_complete.json")
    s.add_argument("--amount")
    s.add_argument("--outcome", choices=["approve", "dispute", "fix", "auto"], default="auto")
    s.add_argument("--wait", type=int, default=600, help="seconds to wait for sandbox buyer approval")

    a = p.parse_args(argv)
    eid = Eid.from_env()
    print(f"(paypal={eid.paypal.mode}, ai={eid.ai.mode}, data={eid.config.data_dir})", file=sys.stderr)
    try:
        if a.cmd == "create":
            text = a.text or (Path(a.file).read_text() if a.file else sys.stdin.read())
            print(summary(eid.create_job(text, a.amount, a.currency)))
        elif a.cmd == "show":
            job = eid.get(a.job)
            print(job.model_dump_json(indent=2) if a.json else summary(job))
        elif a.cmd == "list":
            for j in eid.store.all():
                print(f"{j.id}  {j.state.value:<18} {j.amount} {j.currency}  {j.card.title}")
        elif a.cmd == "pay":
            job = eid.create_payment(a.job)
            print(f"Customer link: {eid.customer_link(job)}\nPayPal approval URL: {job.payment.approve_url}")
        elif a.cmd == "authorize":
            job = wait_authorize(eid, a.job, a.wait) if a.wait else eid.authorize(a.job)
            print(f"Authorized: {job.payment.authorization_id} ({job.payment.authorization_status})")
        elif a.cmd == "evidence":
            items, base = evidence_from_args(a)
            print(summary(eid.submit_evidence(a.job, items, base)))
        elif a.cmd == "packet":
            print(eid.generate_packet(a.job).packet_markdown)
        elif a.cmd == "approve":
            job = eid.approve(a.job, a.note)
            print(f"Captured: {job.payment.capture_id} ({job.payment.capture_status})")
        elif a.cmd == "dispute":
            job = eid.dispute(a.job, a.reason, "fix" if a.fix else "dispute")
            print(f"Voided authorization {job.payment.authorization_id}")
        elif a.cmd == "serve":
            from .server import serve
            serve(eid, a.host, a.port)
        elif a.cmd == "demo":
            demo(eid, a)
    except (EidError, TransitionError, JobNotFound, PayPalError, AIError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0
