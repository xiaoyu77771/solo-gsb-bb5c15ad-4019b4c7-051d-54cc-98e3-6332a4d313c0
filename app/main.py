"""HTTP API for the drinking-water blending service."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from . import db
from .config import get_host, get_port
from .models import (
    Allocation,
    BlendRequest,
    ContingencyAllocation,
    ContingencyInfeasible,
    ContingencyPlan,
    ContingencyRequest,
    InfeasiblePlan,
    OptimalPlan,
    PlanCreateRequest,
)
from .solver import INDICATORS, SourceData, solve_blend, solve_contingency

SolveResponse = Union[OptimalPlan, InfeasiblePlan]
ContingencyResponse = Union[ContingencyPlan, ContingencyInfeasible]


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
        "不可行时返回各指标的可达范围。已保存的 optimal 方案可通过 "
        "POST /api/v1/blends/{plan_id}/contingency 计算供给缩减预案："
        "先最小化总调整量，再最小化总成本，用量向量字典序收尾。"
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


@app.post(
    "/api/v1/blends/{plan_id}/contingency",
    response_model=ContingencyResponse,
)
def plan_contingency(plan_id: int, body: ContingencyRequest) -> JSONResponse:
    """Re-optimize a saved optimal plan under reduced source supply caps.

    The contingency is never persisted and the original plan is left
    untouched.  Target volume, indicator ranges, water quality and costs are
    kept identical; the new blend minimizes total volume change first and
    total cost second, with the id-ascending usage vector as the final
    lexicographic tie-breaker.
    """
    row = db.find_by_id(plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"plan {plan_id} not found")
    if row["status"] != "optimal":
        raise HTTPException(
            status_code=422,
            detail="contingency is only available for plans saved with status 'optimal'",
        )

    original = json.loads(row["response_json"])
    req = BlendRequest.model_validate(json.loads(row["request_json"]))

    original_ids = {a["source_id"] for a in original["blend"]}
    unknown = sorted(set(body.reductions) - original_ids)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown source id(s) not present in the original plan: {unknown}",
        )
    orig_cap = {s.id: s.max_volume for s in req.sources}
    bad = {
        sid: cap
        for sid, cap in body.reductions.items()
        if cap < 0.0 or cap > orig_cap[sid]
    }
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(
                "each new cap must satisfy 0 <= new cap <= original max_volume; "
                f"violating entries: {bad}"
            ),
        )

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
    original_volume = {a["source_id"]: float(a["volume"]) for a in original["blend"]}
    x0 = [original_volume[s.id] for s in sources]
    limits = {}
    for name in INDICATORS:
        rng = getattr(req, name)
        if rng is not None:
            limits[name] = (rng.min, rng.max)

    try:
        result = solve_contingency(
            sources, req.target_volume, limits, np.array(x0, dtype=float),
            dict(body.reductions),
        )
    except RuntimeError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail=str(exc))

    reductions_sorted = {sid: body.reductions[sid] for sid in sorted(body.reductions)}
    if result.status != "optimal":
        out = ContingencyInfeasible(
            plan_id=plan_id,
            message=result.message,
            reductions=reductions_sorted,
            achievable_ranges=result.achievable_ranges,
        )
        return JSONResponse(_round6(out.model_dump()))

    blend = [
        ContingencyAllocation(
            source_id=s.id,
            volume=float(v),
            cost=float(v) * s.cost,
            volume_delta=float(v) - x0[k],
            original_volume=x0[k],
            original_max_volume=s.max_volume,
            effective_max_volume=float(body.reductions.get(s.id, s.max_volume)),
        )
        for k, (s, v) in enumerate(zip(sources, result.volumes))
    ]
    original_cost = float(original["total_cost"])
    out = ContingencyPlan(
        plan_id=plan_id,
        total_volume=req.target_volume,
        original_total_cost=original_cost,
        total_cost=result.total_cost,
        cost_delta=result.total_cost - original_cost,
        total_adjustment=result.total_adjustment,
        reductions=reductions_sorted,
        blend=blend,
        indicators=result.indicators,
    )
    return JSONResponse(_round6(out.model_dump()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=get_host(), port=get_port())
