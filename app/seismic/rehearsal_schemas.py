from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateParams(BaseModel):
    model_version: str = Field(..., min_length=1, max_length=40)
    grid_step_km: float = Field(..., gt=0, le=100)
    radius_km: float = Field(..., gt=0, le=1000)
    pga_weight: float = Field(..., ge=0, le=1)
    accepted_score_threshold: float = Field(..., ge=0, le=1)


class RehearsalCreate(BaseModel):
    candidate: CandidateParams
    event_ids: list[int] | None = Field(default=None, max_length=50)
    reason: str = Field(default="", max_length=300)


class RehearsalDecision(BaseModel):
    reason: str = Field(default="", max_length=300)
