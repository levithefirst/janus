"""JSON-file job store: one file per job at <data_dir>/jobs/<job_id>/job.json."""
from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .logs import now
from .models import EvidenceItem, GapReport, JobCard
from .state import State


class Payment(BaseModel):
    order_id: Optional[str] = None
    approve_url: Optional[str] = None
    authorization_id: Optional[str] = None
    authorization_status: Optional[str] = None
    capture_id: Optional[str] = None
    capture_status: Optional[str] = None
    voided: bool = False


class Job(BaseModel):
    id: str
    state: State
    created_at: str
    updated_at: str
    source_text: str
    card: JobCard
    amount: str
    currency: str
    payment: Payment = Field(default_factory=Payment)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    gap_report: Optional[GapReport] = None
    packet_markdown: Optional[str] = None
    decision: Optional[dict[str, Any]] = None
    events: list[dict[str, Any]] = Field(default_factory=list)


class JobNotFound(Exception):
    pass


class Store:
    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def new_id() -> str:
        return "job_" + secrets.token_hex(4)

    def job_dir(self, job_id: str) -> Path:
        return self.root / job_id

    def save(self, job: Job) -> None:
        job.updated_at = now()
        d = self.job_dir(job.id)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "job.json.tmp"
        tmp.write_text(job.model_dump_json(indent=2))
        os.replace(tmp, d / "job.json")

    def load(self, job_id: str) -> Job:
        if not re.fullmatch(r"job_[0-9a-f]{8}", job_id or ""):
            raise JobNotFound(f"{job_id!r} (job ids look like job_1a2b3c4d)")
        p = self.job_dir(job_id) / "job.json"
        if not p.is_file():
            raise JobNotFound(f"{job_id} (see `python -m eid list`)")
        return Job.model_validate_json(p.read_text())

    def all(self) -> list[Job]:
        jobs = [Job.model_validate_json(p.read_text()) for p in self.root.glob("*/job.json")]
        return sorted(jobs, key=lambda j: j.created_at)

    def find_by_order(self, order_id: str) -> Job:
        for job in self.all():
            if job.payment.order_id == order_id:
                return job
        raise JobNotFound(f"no job for PayPal order {order_id}")
