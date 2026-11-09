import json

import pytest

from conftest import EXAMPLES
from eid.paypal import PayPalError
from eid.service import EidError
from eid.state import State, TransitionError


def evidence(name):
    return json.loads((EXAMPLES / name).read_text())["items"]


def to_authorized(eid, text):
    job = eid.create_job(text)
    assert job.state == State.CREATED
    job = eid.create_payment(job.id)
    assert job.state == State.PAYMENT_PENDING and job.payment.order_id
    job = eid.authorize(job.id)
    assert job.state == State.AUTHORIZED and job.payment.authorization_id
    return job


def test_job_card_from_text(eid, job_text):
    job = eid.create_job(job_text)
    assert job.amount == "180.00" and job.currency == "USD"
    assert len(job.card.checklist) == 4
    covered = {c for e in job.card.required_evidence for c in e.checklist_item_ids}
    assert covered == {c.id for c in job.card.checklist}
    assert eid.get(job.id).card == job.card  # persisted


def test_amount_required_when_text_has_no_price(eid):
    with pytest.raises(EidError, match="no price"):
        eid.create_job("Mow the lawn.")
    assert eid.create_job("Mow the lawn.", amount="40").amount == "40.00"


def test_full_loop_approve_captures(eid, job_text):
    job = to_authorized(eid, job_text)
    job = eid.submit_evidence(job.id, evidence("evidence_complete.json"), EXAMPLES)
    assert job.state == State.EVIDENCE_SUBMITTED and len(job.evidence) == 6
    assert all(e.sha256 for e in job.evidence)
    job = eid.generate_packet(job.id)
    assert job.state == State.PACKET_READY
    assert job.gap_report.recommended_action == "approve"
    assert "# Acceptance packet" in job.packet_markdown
    assert (eid.store.job_dir(job.id) / "acceptance_packet.md").is_file()
    job = eid.approve(job.id)
    assert job.state == State.CAPTURED and job.payment.capture_status == "COMPLETED"
    actions = [e["action"] for e in job.events]
    for a in ("order_created", "authorized", "gap_report_decision", "packet_generated", "captured"):
        assert a in actions
    log = (eid.config.data_dir / "eid.log.jsonl").read_text()
    assert '"capture_authorization"' in log and '"job_card_decision"' in log


def test_partial_evidence_then_dispute_voids(eid, job_text):
    job = to_authorized(eid, job_text)
    job = eid.submit_evidence(job.id, evidence("evidence_partial.json"), EXAMPLES)
    job = eid.generate_packet(job.id)
    assert job.gap_report.recommended_action == "request_more_evidence"
    assert job.gap_report.gaps
    job = eid.dispute(job.id, "Missing under-sink photo", kind="fix")
    assert job.state == State.VOIDED and job.payment.voided
    assert job.decision["decision"] == "fix"
    with pytest.raises(TransitionError):
        eid.approve(job.id)  # can't capture a voided authorization


def test_provider_can_add_evidence_after_gap_report(eid, job_text):
    job = to_authorized(eid, job_text)
    eid.submit_evidence(job.id, evidence("evidence_partial.json"), EXAMPLES)
    eid.generate_packet(job.id)
    job = eid.submit_evidence(job.id, evidence("evidence_complete.json"), EXAMPLES)
    assert job.state == State.EVIDENCE_SUBMITTED
    assert eid.generate_packet(job.id).gap_report.recommended_action == "approve"


@pytest.mark.parametrize("step", ["authorize", "evidence", "packet", "approve", "dispute"])
def test_out_of_order_steps_are_rejected(eid, job_text, step):
    job = eid.create_job(job_text)
    calls = {
        "authorize": lambda: eid.authorize(job.id),
        "evidence": lambda: eid.submit_evidence(job.id, [{"note": "x"}]),
        "packet": lambda: eid.generate_packet(job.id),
        "approve": lambda: eid.approve(job.id),
        "dispute": lambda: eid.dispute(job.id, "bad"),
    }
    with pytest.raises(TransitionError):
        calls[step]()
    assert eid.get(job.id).state == State.CREATED


def test_approve_requires_packet(eid, job_text):
    job = to_authorized(eid, job_text)
    eid.submit_evidence(job.id, [{"note": "done"}])
    with pytest.raises(TransitionError):
        eid.approve(job.id)


def test_unknown_requirement_and_missing_file_rejected(eid, job_text):
    job = to_authorized(eid, job_text)
    with pytest.raises(EidError, match="unknown requirement_id"):
        eid.submit_evidence(job.id, [{"requirement_id": "E99", "note": "x"}])
    with pytest.raises(EidError, match="not found"):
        eid.submit_evidence(job.id, [{"kind": "photo", "file": "nope.png"}])
    assert eid.get(job.id).state == State.AUTHORIZED


def test_failed_capture_can_be_retried(eid, job_text, monkeypatch):
    job = to_authorized(eid, job_text)
    eid.submit_evidence(job.id, evidence("evidence_complete.json"), EXAMPLES)
    eid.generate_packet(job.id)
    real = eid.paypal.capture_authorization

    def boom(*a, **k):
        raise PayPalError("network down", 503)
    monkeypatch.setattr(eid.paypal, "capture_authorization", boom)
    with pytest.raises(PayPalError):
        eid.approve(job.id)
    assert eid.get(job.id).state == State.APPROVED
    monkeypatch.setattr(eid.paypal, "capture_authorization", real)
    assert eid.approve(job.id).state == State.CAPTURED
