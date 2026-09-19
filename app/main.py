"""HTTP API for the drinking-water blending service."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from . import db
from .config import get_host, get_port
from .models import (
    Allocation,
    BlendRequest,
    InfeasiblePlan,
    OptimalPlan,
    PlanCreateRequest,
)
from .solver import INDICATORS, SourceData, solve_blend

SolveResponse = Union[OptimalPlan, InfeasiblePlan]


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()  # create the SQLite database/schema on startup
    yield


app = FastAPI(
    title="Drinking Water Blending API",
    version="1.0.0",
    description=(
        "最低成本饮用水混配优化：在满足钙 / 镁 / 碱度区间与水源供给上限的"
        "前提下最小化总成本；成本并列时取按水源 ID 排序后用量向量字典序最小者；"
        "不可行时返回各指标的可达范围。"
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _round6(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round6(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round6(v) for v in obj]
    return obj


def _solve(req: BlendRequest) -> Tuple[List[SourceData], Any]:
    sources = sorted(
        (
            SourceData(
                id=s.id,
                max_volume=s.max_volume,
                cost=s.cost,
                calcium=s.calcium,
                magnesium=s.magnesium,
                alkalinity=s.alkalinity,
            )
            for s in req.sources
        ),
        key=lambda s: s.id,
    )
    limits = {}
    for name in INDICATORS:
        rng = getattr(req, name)
        if rng is not None:
            limits[name] = (rng.min, rng.max)
    try:
        result = solve_blend(sources, req.target_volume, limits)
    except RuntimeError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc))
    return sources, result


def _build_body(sources, req: BlendRequest, result, plan_id, created_at) -> Dict[str, Any]:
    if result.status == "optimal":
        body = OptimalPlan(
            plan_id=plan_id,
            created_at=created_at,
            total_volume=req.target_volume,
            total_cost=result.total_cost,
            blend=[
                Allocation(source_id=s.id, volume=v, cost=v * s.cost)
                for s, v in zip(sources, result.volumes)
            ],
            indicators=result.indicators,
        )
    else:
        body = InfeasiblePlan(
            plan_id=plan_id,
            created_at=created_at,
            message=result.message,
            achievable_ranges=result.achievable_ranges,
        )
    return _round6(body.model_dump())


def _canonical_request(req: PlanCreateRequest) -> Tuple[str, str]:
    """Canonical JSON of the blending parameters (key excluded) + its hash."""
    payload = req.model_dump(mode="json")
    payload.pop("idempotency_key", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest(), canonical


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.get("/")
def root() -> Dict[str, str]:
    return {
        "service": "drinking-water-blending-api",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/blends/solve", response_model=SolveResponse)
def solve_only(req: BlendRequest) -> SolveResponse:
    """Solve a blending problem without persisting anything."""
    sources, result = _solve(req)
    return _build_body(sources, req, result, plan_id=None, created_at=None)


@app.post("/api/v1/blends", response_model=SolveResponse)
def create_plan(
    req: PlanCreateRequest,
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> JSONResponse:
    """Solve and persist a plan, idempotently.

    The idempotency key may be supplied either in the request body
    (``idempotency_key``) or via the ``Idempotency-Key`` header.  Replaying a
    key with the identical request returns the stored plan; replaying it with
    a different request fails with 409.
    """
    key = req.idempotency_key or idempotency_key
    if not key:
        raise HTTPException(
            status_code=422,
            detail=(
                "an idempotency key is required: set 'idempotency_key' in the "
                "request body or send the 'Idempotency-Key' header"
            ),
        )
    req_hash, canonical = _canonical_request(req)

    existing = db.find_by_key(key)
    if existing is not None:
        if existing["request_hash"] != req_hash:
            raise HTTPException(
                status_code=409,
                detail="idempotency key was already used with a different request",
            )
        return JSONResponse(
            json.loads(existing["response_json"]),
            headers={"X-Idempotent-Replay": "true"},
        )

    sources, result = _solve(req)
    body = _build_body(sources, req, result, plan_id=None, created_at=_utcnow())
    created, _, stored, existing_hash = db.save_plan(
        key, req_hash, canonical, body["status"], body
    )
    if not created:  # lost a concurrent-insert race on the same key
        if existing_hash != req_hash:
            raise HTTPException(
                status_code=409,
                detail="idempotency key was already used with a different request",
            )
        return JSONResponse(stored, headers={"X-Idempotent-Replay": "true"})
    return JSONResponse(stored, status_code=201)


@app.get("/api/v1/blends/{plan_id}", response_model=SolveResponse)
def get_plan(plan_id: int) -> JSONResponse:
    """Fetch a previously saved plan by its id."""
    row = db.find_by_id(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
    return JSONResponse(json.loads(row["response_json"]))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=get_host(), port=get_port())
