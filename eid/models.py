"""Data shapes. The AI outputs (JobCard, GapReport) are validated with structured outputs."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- AI output 1: job card ----------

class Price(BaseModel):
    amount: str = Field(description="Decimal string, e.g. '180.00'")
    currency: str = Field(description="ISO 4217 code, e.g. 'USD'")


class ChecklistItem(BaseModel):
    id: str = Field(description="Short id: C1, C2, ...")
    description: str = Field(description="One concrete deliverable the customer is paying for")
    acceptance_criteria: str = Field(description="How a customer can tell this item is done")


class EvidenceRequirement(BaseModel):
    id: str = Field(description="Short id: E1, E2, ...")
    description: str = Field(description="Exactly what the provider must submit")
    kind: Literal["photo", "video", "document", "note", "measurement"]
    checklist_item_ids: list[str] = Field(description="Checklist items this evidence proves")


class JobCard(BaseModel):
    title: str
    summary: str
    service_category: str
    location: Optional[str]
    scheduled_for: Optional[str]
    price: Optional[Price] = Field(description="Only if the text states a price, otherwise null")
    scope: list[str]
    exclusions: list[str]
    assumptions: list[str] = Field(description="Things inferred, not stated, that both sides should confirm")
    checklist: list[ChecklistItem]
    required_evidence: list[EvidenceRequirement]


# ---------- AI output 2: gap report ----------

class RequirementResult(BaseModel):
    requirement_id: str
    status: Literal["satisfied", "partial", "missing"]
    evidence_ids: list[str]
    reasoning: str


class ChecklistResult(BaseModel):
    item_id: str
    status: Literal["met", "unclear", "not_met"]
    reasoning: str


class GapReport(BaseModel):
    requirements: list[RequirementResult]
    checklist: list[ChecklistResult]
    gaps: list[str] = Field(description="Plain-language list of what is missing or unproven")
    recommended_action: Literal["approve", "request_more_evidence", "request_fix"]
    confidence: Literal["low", "medium", "high"]
    rationale: str


# ---------- provider input ----------

class EvidenceItem(BaseModel):
    id: str = ""                      # assigned on submit: V1, V2, ...
    requirement_id: Optional[str] = None  # provider's claim of which requirement it covers
    kind: Literal["photo", "video", "document", "note", "measurement"] = "note"
    note: str = ""
    file: Optional[str] = None        # path inside the job's data dir after submit
    media_type: Optional[str] = None
    sha256: Optional[str] = None
    submitted_at: str = ""
