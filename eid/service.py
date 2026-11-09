"""Orchestration: each public method is one step of the loop and one guarded state change."""
from __future__ import annotations

import hashlib
import mimetypes
import shutil
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Optional

from .ai import ClaudeAI, MockAI
from .config import Config
from .logs import log_event, now, setup
from .models import EvidenceItem
from .paypal import PayPalMock, PayPalSandbox
from .state import State, check
from .store import Job, Store


class EidError(Exception):
    pass


def _money(amount: str) -> str:
    try:
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
    except InvalidOperation:
        raise EidError(f"invalid amount {amount!r}")
    if d <= 0:
        raise EidError("amount must be positive")
    return str(d)


class Eid:
    def __init__(self, config: Config, ai=None, paypal=None):
        self.config = config
        setup(config.data_dir)
        self.store = Store(config.data_dir)
        self.ai = ai or (ClaudeAI(config.model) if config.ai_mode == "claude" else MockAI())
        self.paypal = paypal or (PayPalSandbox(config.paypal_client_id, config.paypal_client_secret)
                                 if config.paypal_mode == "sandbox" else PayPalMock(config.data_dir))

    @classmethod
    def from_env(cls) -> "Eid":
        return cls(Config.from_env())

    # --- helpers ---

    def _move(self, job: Job, target: State, **data) -> None:
        check(job.state, target)
        rec = log_event("state", "transition", job.id, from_state=job.state.value, to_state=target.value, **data)
        job.events.append(rec)
        job.state = target
        self.store.save(job)

    def _record(self, job: Job, category: str, action: str, **data) -> None:
        job.events.append(log_event(category, action, job.id, **data))

    def get(self, job_id: str) -> Job:
        return self.store.load(job_id)

    def customer_link(self, job: Job) -> str:
        return f"{self.config.base_url}/jobs/{job.id}/pay"

    # --- 1 + 2: create job, AI job card ---

    def create_job(self, text: str, amount: Optional[str] = None, currency: Optional[str] = None) -> Job:
        text = text.strip()
        if not text:
            raise EidError("job text is empty")
        job_id = self.store.new_id()
        card = self.ai.build_job_card(text, job_id=job_id)
        if amount is None and card.price is None:
            raise EidError("no price found in the job text; pass an amount")
        amount = _money(amount if amount is not None else card.price.amount)
        currency = (currency or (card.price.currency if card.price else None) or "USD").upper()
        job = Job(id=job_id, state=State.CREATED, created_at=now(), updated_at=now(), source_text=text,
                  card=card, amount=amount, currency=currency)
        self._record(job, "ai", "job_card_decision", title=card.title, checklist=[c.id for c in card.checklist],
                     required_evidence=[f"{e.id}:{e.kind}" for e in card.required_evidence],
                     amount=amount, currency=currency)
        self._record(job, "state", "created", state=State.CREATED.value)
        self.store.save(job)
        return job

    # --- 3: PayPal order + authorization ---

    def create_payment(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.state == State.PAYMENT_PENDING:  # idempotent: re-issue the same link
            return job
        check(job.state, State.PAYMENT_PENDING)
        base = self.config.base_url
        order = self.paypal.create_order(job.id, job.amount, job.currency, job.card.title,
                                         return_url=f"{base}/paypal/return", cancel_url=f"{base}/paypal/cancel")
        job.payment.order_id, job.payment.approve_url = order["order_id"], order["approve_url"]
        self._record(job, "paypal", "order_created", order_id=order["order_id"], approve_url=order["approve_url"])
        self._move(job, State.PAYMENT_PENDING, customer_link=self.customer_link(job))
        return job

    def order_status(self, job_id: str) -> str:
        job = self.get(job_id)
        if not job.payment.order_id:
            raise EidError("no PayPal order yet; create the payment first")
        return self.paypal.get_order(job.id, job.payment.order_id).get("status", "UNKNOWN")

    def authorize(self, job_id: str) -> Job:
        job = self.get(job_id)
        check(job.state, State.AUTHORIZED)
        status = self.order_status(job_id)
        if status != "APPROVED":
            raise EidError(f"customer has not approved the PayPal order yet (status {status}); "
                           f"send them {self.customer_link(job)}")
        auth = self.paypal.authorize_order(job.id, job.payment.order_id)
        job.payment.authorization_id, job.payment.authorization_status = auth["authorization_id"], auth["status"]
        self._record(job, "paypal", "authorized", authorization_id=auth["authorization_id"],
                     status=auth["status"], expiration_time=auth.get("expiration_time"))
        self._move(job, State.AUTHORIZED)
        return job

    # --- 4: evidence ---

    def submit_evidence(self, job_id: str, items: Iterable[dict], base_dir: Path = Path(".")) -> Job:
        job = self.get(job_id)
        check(job.state, State.EVIDENCE_SUBMITTED)
        items = list(items)
        if not items:
            raise EidError("no evidence items given")
        valid_reqs = {r.id for r in job.card.required_evidence}
        ev_dir = self.store.job_dir(job.id) / "evidence"
        added = []
        for raw in items:
            item = EvidenceItem.model_validate({k: v for k, v in raw.items() if k != "id"})
            if item.requirement_id and item.requirement_id not in valid_reqs:
                raise EidError(f"unknown requirement_id {item.requirement_id!r}; valid: {sorted(valid_reqs)}")
            item.id = f"V{len(job.evidence) + 1}"
            item.submitted_at = now()
            if item.file:
                src = Path(item.file)
                src = src if src.is_absolute() else base_dir / src
                if not src.is_file():
                    raise EidError(f"evidence file not found: {src}")
                ev_dir.mkdir(parents=True, exist_ok=True)
                dest = ev_dir / f"{item.id}{src.suffix.lower()}"
                shutil.copyfile(src, dest)
                item.file = str(dest.relative_to(self.store.job_dir(job.id)))
                item.media_type = item.media_type or mimetypes.guess_type(src.name)[0] or "application/octet-stream"
                item.sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
            if not item.file and not item.note:
                raise EidError("each evidence item needs a note or a file")
            job.evidence.append(item)
            added.append(item.id)
        self._record(job, "state", "evidence_added", evidence_ids=added)
        self._move(job, State.EVIDENCE_SUBMITTED, total_evidence=len(job.evidence))
        return job

    # --- 5: AI gap report + acceptance packet ---

    def generate_packet(self, job_id: str) -> Job:
        job = self.get(job_id)
        check(job.state, State.PACKET_READY)
        report = self.ai.check_evidence(job.card, job.evidence, self.store.job_dir(job.id), job_id=job.id)
        self._record(job, "ai", "gap_report_decision", recommended_action=report.recommended_action,
                     confidence=report.confidence, gaps=report.gaps,
                     requirements={r.requirement_id: r.status for r in report.requirements},
                     checklist={c.item_id: c.status for c in report.checklist})
        packet = self.ai.build_packet(job.id, job.card, job.amount, job.currency, job.evidence, report)
        job.gap_report, job.packet_markdown = report, packet
        (self.store.job_dir(job.id) / "acceptance_packet.md").write_text(packet + "\n")
        self._record(job, "ai", "packet_generated", chars=len(packet))
        self._move(job, State.PACKET_READY, recommended_action=report.recommended_action)
        return job

    # --- 6: customer decision -> capture or void ---

    def approve(self, job_id: str, note: str = "") -> Job:
        job = self.get(job_id)
        if job.state != State.APPROVED:  # APPROVED means a previous capture attempt failed: retry it
            check(job.state, State.APPROVED)
            job.decision = {"decision": "approve", "note": note, "at": now(),
                            "ai_recommended": job.gap_report.recommended_action if job.gap_report else None}
            self._move(job, State.APPROVED, note=note)
        check(job.state, State.CAPTURED)
        cap = self.paypal.capture_authorization(job.id, job.payment.authorization_id)
        job.payment.capture_id, job.payment.capture_status = cap["capture_id"], cap["status"]
        self._record(job, "paypal", "captured", capture_id=cap["capture_id"], status=cap["status"])
        self._move(job, State.CAPTURED)
        return job

    def dispute(self, job_id: str, reason: str, kind: str = "dispute") -> Job:
        if kind not in ("dispute", "fix"):
            raise EidError("kind must be 'dispute' or 'fix'")
        if not reason.strip():
            raise EidError("a reason is required")
        job = self.get(job_id)
        if job.state != State.DISPUTED:  # DISPUTED means a previous void attempt failed: retry it
            check(job.state, State.DISPUTED)
            job.decision = {"decision": kind, "reason": reason, "at": now(),
                            "ai_recommended": job.gap_report.recommended_action if job.gap_report else None}
            self._move(job, State.DISPUTED, kind=kind, reason=reason)
        check(job.state, State.VOIDED)
        res = self.paypal.void_authorization(job.id, job.payment.authorization_id)
        job.payment.voided = True
        self._record(job, "paypal", "voided", authorization_id=job.payment.authorization_id, status=res["status"])
        self._move(job, State.VOIDED)
        return job
