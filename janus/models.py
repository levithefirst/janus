"""Data shapes. The two AI outputs (CompiledCondition, Verdict) are validated with structured outputs."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- AI output 1: the compiled condition ----------

class Requirement(BaseModel):
    id: str = Field(description="Short id: R1, R2, ...")
    statement: str = Field(description="One thing that must be true about the delivered item")
    check: str = Field(description="What to look for in the buyer's photo to confirm or refute it")
    critical: bool = Field(description="True if failing this requirement means the condition is not met")


class CompiledCondition(BaseModel):
    summary: str = Field(description="The condition restated in one plain sentence both parties agree to")
    requirements: list[Requirement]
    evidence_instructions: str = Field(description="One or two sentences telling the buyer exactly what photo to take")
    not_verifiable: list[str] = Field(description="Parts of the condition a photo cannot prove; empty if none")


# ---------- AI output 2: the verdict ----------

class RequirementCheck(BaseModel):
    requirement_id: str
    result: Literal["met", "not_met", "cannot_tell"]
    observation: str = Field(description="What the evidence actually shows for this requirement")
    confidence: Optional[float] = Field(default=None, description="Probability 0.0 to 1.0 that this "
                                        "requirement's result is correct")


class Verdict(BaseModel):
    verdict: Literal["met", "not_met", "uncertain"]
    confidence: float = Field(description="Probability from 0.0 to 1.0 that the overall verdict is correct")
    checks: list[RequirementCheck]
    summary: str = Field(description="One or two plain sentences for buyer and seller")
    next_step: Optional[str] = Field(description="If uncertain: the exact photo that would settle it; else null")
    # Job mode only (before/after photos). None in threshold mode.
    scene_match: Optional[Literal["same", "different", "unclear"]] = None
    edit_signs: Optional[Literal["none", "possible", "likely"]] = None
    integrity_note: Optional[str] = None


class JobVerdict(Verdict):
    """The structured output asked of the model for a job packet: the job fields are required."""
    scene_match: Literal["same", "different", "unclear"] = Field(
        description="Do the before and after photos show the same place (room, fixtures, layout)?")
    edit_signs: Literal["none", "possible", "likely"] = Field(
        description="Signs that a photo was digitally edited or generated (cloned areas, pasted objects, "
                    "inconsistent shadows or lighting, warped straight lines, AI texture)")
    integrity_note: str = Field(description="One sentence on the scene match and edit signs")


# ---------- inputs ----------

class Listing(BaseModel):
    title: str
    description: str = ""
    price: str
    currency: str = "USD"
    photos: list[str] = Field(default_factory=list)  # paths inside the threshold's data dir


class EvidenceItem(BaseModel):
    id: str = ""                        # assigned on submit: V1, V2, ...
    by: Literal["buyer", "seller"] = "buyer"   # customer, or provider re-uploading
    phase: Optional[Literal["before", "after"]] = None  # job mode: provider's before / after photos
    note: str = ""                      # sanitised; untrusted data
    quality: dict = Field(default_factory=dict)  # width, height, sharpness, brightness, ok, reasons
    exif: dict = Field(default_factory=dict)     # present, taken_at, offset, gps (rounded to ~100 m)
    phash: Optional[str] = None
    flags: list[str] = Field(default_factory=list)
    file: Optional[str] = None          # path inside the threshold's data dir
    media_type: Optional[str] = None
    sha256: Optional[str] = None
    submitted_at: str = ""
