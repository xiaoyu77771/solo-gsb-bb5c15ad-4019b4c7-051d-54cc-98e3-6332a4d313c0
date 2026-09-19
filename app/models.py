"""Pydantic request/response schemas."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class IndicatorRange(BaseModel):
    """Allowed concentration interval for one indicator (mg/L)."""

    min: float = Field(..., ge=0, description="lower bound, mg/L")
    max: float = Field(..., ge=0, description="upper bound, mg/L")

    @model_validator(mode="after")
    def _ordered(self) -> "IndicatorRange":
        if self.min > self.max:
            raise ValueError("min must be <= max")
        return self


class Source(BaseModel):
    """One candidate water source."""

    id: str = Field(..., min_length=1, max_length=64, description="unique source id")
    max_volume: float = Field(..., ge=0, description="available volume, m3")
    cost: float = Field(..., ge=0, description="cost per m3")
    calcium: float = Field(..., ge=0, description="calcium concentration, mg/L")
    magnesium: float = Field(..., ge=0, description="magnesium concentration, mg/L")
    alkalinity: float = Field(..., ge=0, description="alkalinity, mg/L as CaCO3")

    @field_validator("id", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return str(v).strip()


class BlendRequest(BaseModel):
    """Blending problem definition."""

    target_volume: float = Field(..., gt=0, description="required blend volume, m3")
    calcium: Optional[IndicatorRange] = None
    magnesium: Optional[IndicatorRange] = None
    alkalinity: Optional[IndicatorRange] = None
    sources: List[Source] = Field(..., min_length=2, max_length=6)

    @model_validator(mode="after")
    def _unique_ids(self) -> "BlendRequest":
        ids = [s.id for s in self.sources]
        if len(set(ids)) != len(ids):
            raise ValueError("source ids must be unique")
        return self


class PlanCreateRequest(BlendRequest):
    """Blend request plus the idempotency key used to persist the plan."""

    idempotency_key: Optional[str] = Field(default=None, min_length=1, max_length=255)


class Allocation(BaseModel):
    source_id: str
    volume: float
    cost: float


class OptimalPlan(BaseModel):
    status: Literal["optimal"] = "optimal"
    plan_id: Optional[int] = None
    created_at: Optional[str] = None
    total_volume: float
    total_cost: float
    blend: List[Allocation]
    indicators: Dict[str, float]


class InfeasiblePlan(BaseModel):
    status: Literal["infeasible"] = "infeasible"
    plan_id: Optional[int] = None
    created_at: Optional[str] = None
    message: str
    achievable_ranges: Dict[str, Any]


class ContingencyRequest(BaseModel):
    """Supply-cut contingency: existing source id -> new supply cap."""

    reductions: Dict[str, float] = Field(
        ...,
        min_length=1,
        description="已有水源 ID 到新供给上限的映射；未列出的水源沿用原上限",
    )

    @field_validator("reductions")
    @classmethod
    def _finite(cls, v: Dict[str, float]) -> Dict[str, float]:
        for sid, cap in v.items():
            if not isinstance(sid, str) or not sid.strip():
                raise ValueError("source ids must be non-empty strings")
            if cap != cap or cap in (float("inf"), float("-inf")):
                raise ValueError(f"new cap for source {sid!r} must be a finite number")
        return v


class ContingencyAllocation(BaseModel):
    source_id: str
    volume: float
    cost: float
    volume_delta: float          # new volume - original volume
    original_volume: float
    original_max_volume: float
    effective_max_volume: float  # the cap in effect after the reduction


class ContingencyPlan(BaseModel):
    status: Literal["optimal"] = "optimal"
    plan_id: int
    total_volume: float
    original_total_cost: float
    total_cost: float
    cost_delta: float            # new total cost - original total cost
    total_adjustment: float      # sum of |volume_delta|
    reductions: Dict[str, float]
    blend: List[ContingencyAllocation]
    indicators: Dict[str, float]


class ContingencyInfeasible(BaseModel):
    status: Literal["infeasible"] = "infeasible"
    plan_id: int
    message: str
    reductions: Dict[str, float]
    achievable_ranges: Dict[str, Any]
