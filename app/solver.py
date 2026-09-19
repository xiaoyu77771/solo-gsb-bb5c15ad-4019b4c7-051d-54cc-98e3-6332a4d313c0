"""Linear-programming blend optimizer.

Model
-----
Variables x_i >= 0: volume taken from source i.

    minimize    sum(cost_i * x_i)
    subject to  sum(x_i)                       = target_volume
                lo_j * target_volume <= sum(conc_ij * x_i) <= hi_j * target_volume
                x_i                            <= max_volume_i

When several optima share the same minimal cost, the usage vector ordered by
ascending source id is minimized lexicographically via a sequence of LPs.

If the problem is infeasible, the achievable (min, max) concentration range of
every indicator is reported, projected onto the supply/volume feasible set.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import linprog

INDICATORS: Tuple[str, ...] = ("calcium", "magnesium", "alkalinity")

# Half-width (relative) of the interval used to pin a variable after its
# lexicographic minimum has been found.  Comfortably above solver noise and
# below the 6-decimal output rounding for typical magnitudes.
_FIX_RTOL = 1e-8
# Fallback cost slack (relative) if the exact cost-equality refinement fails.
_COST_RTOL = 1e-9


@dataclass
class SourceData:
    id: str
    max_volume: float
    cost: float
    calcium: float
    magnesium: float
    alkalinity: float


@dataclass
class SolveOutput:
    status: str  # "optimal" | "infeasible"
    volumes: Optional[np.ndarray] = None       # aligned with the sorted sources
    total_cost: Optional[float] = None
    indicators: Optional[Dict[str, float]] = None
    achievable_ranges: Optional[Dict[str, dict]] = None
    message: Optional[str] = None


def solve_blend(
    sources: List[SourceData],
    target_volume: float,
    limits: Dict[str, Tuple[float, float]],
) -> SolveOutput:
    """Solve the blending LP.  ``limits`` maps indicator name -> (min, max)."""
    sources = sorted(sources, key=lambda s: s.id)
    n = len(sources)
    costs = np.array([s.cost for s in sources], dtype=float)
    bounds = [(0.0, float(s.max_volume)) for s in sources]

    A_eq = np.ones((1, n))
    b_eq = np.array([float(target_volume)])

    rows, rhs = [], []
    for name in INDICATORS:
        rng = limits.get(name)
        if rng is None:
            continue
        lo, hi = rng
        conc = np.array([getattr(s, name) for s in sources], dtype=float)
        rows.append(-conc)
        rhs.append(-lo * target_volume)
        rows.append(conc)
        rhs.append(hi * target_volume)
    A_ub = np.asarray(rows) if rows else None
    b_ub = np.asarray(rhs) if rhs else None

    res = linprog(
        costs, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
        bounds=bounds, method="highs",
    )
    if res.status == 2:  # provably infeasible
        return _infeasible(sources, target_volume, limits, bounds)
    if res.status != 0:
        raise RuntimeError(f"LP solver failed (status {res.status}): {res.message}")

    x = _lexicographic_min(costs, res.x, A_ub, b_ub, A_eq, b_eq, bounds)
    indicators = {
        name: float(np.array([getattr(s, name) for s in sources]) @ x) / target_volume
        for name in INDICATORS
    }
    return SolveOutput(
        status="optimal",
        volumes=x,
        total_cost=float(costs @ x),
        indicators=indicators,
    )


def _lexicographic_min(costs, x_star, A_ub, b_ub, A_eq, b_eq, bounds) -> np.ndarray:
    """Among all cost-optimal solutions, minimize (x_1, ..., x_n)
    lexicographically (indices follow the id-sorted source order)."""
    n = len(costs)
    # Pin the optimal cost as an equality; x_star satisfies it by construction.
    cost_value = float(costs @ x_star)
    A_eq2 = np.vstack([A_eq, costs.reshape(1, -1)])
    b_eq2 = np.append(b_eq, cost_value)
    try:
        return _sequential_min(n, A_ub, b_ub, A_eq2, b_eq2, bounds)
    except RuntimeError:
        # Fallback: relax the cost pin to a tight inequality.
        tol = _COST_RTOL * max(1.0, abs(cost_value))
        cost_row = costs.reshape(1, -1)
        A_ub2 = cost_row if A_ub is None else np.vstack([A_ub, cost_row])
        b_ub2 = (
            np.array([cost_value + tol])
            if b_ub is None
            else np.append(b_ub, cost_value + tol)
        )
        return _sequential_min(n, A_ub2, b_ub2, A_eq, b_eq, bounds)


def _sequential_min(n, A_ub, b_ub, A_eq, b_eq, bounds) -> np.ndarray:
    bnds = list(bounds)
    x = np.zeros(n)
    for k in range(n):
        obj = np.zeros(n)
        obj[k] = 1.0
        r = linprog(obj, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                    bounds=bnds, method="highs")
        if r.status != 0:
            raise RuntimeError(f"lexicographic refinement failed at index {k}: {r.message}")
        lo0, hi0 = bounds[k]
        v = min(max(float(r.x[k]), lo0), hi0)
        x[k] = v
        eps = _FIX_RTOL * max(1.0, abs(v))
        bnds[k] = (max(lo0, v - eps), min(hi0, v + eps))
    return x


def _infeasible(sources, target_volume, limits, bounds) -> SolveOutput:
    """Explain infeasibility with per-indicator achievable ranges."""
    total_supply = sum(hi for _, hi in bounds)
    achievable: Dict[str, dict] = {}
    messages: List[str] = []

    volume = float(target_volume)
    if total_supply < target_volume * (1.0 - 1e-9):
        messages.append(
            f"total available volume {total_supply:g} is below the target {target_volume:g}"
        )
        achievable["volume"] = {"requested": float(target_volume), "max": float(total_supply)}
        volume = float(total_supply)

    if volume > 0:
        n = len(sources)
        A_eq = np.ones((1, n))
        b_eq = np.array([volume])
        for name in INDICATORS:
            conc = np.array([getattr(s, name) for s in sources], dtype=float)
            lo_res = linprog(conc, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
            hi_res = linprog(-conc, A_eq=A_eq, b_eq=b_eq, bounds=bounds, method="highs")
            entry: Dict[str, float] = {}
            if lo_res.status == 0:
                entry["min"] = float(lo_res.fun) / volume
            if hi_res.status == 0:
                entry["max"] = float(-hi_res.fun) / volume
            req = limits.get(name)
            if req is not None:
                entry["requested_min"], entry["requested_max"] = float(req[0]), float(req[1])
                if "max" in entry and entry["max"] < req[0]:
                    messages.append(
                        f"{name}: requested minimum {req[0]:g} exceeds the achievable "
                        f"maximum {entry['max']:.6g}"
                    )
                if "min" in entry and entry["min"] > req[1]:
                    messages.append(
                        f"{name}: requested maximum {req[1]:g} is below the achievable "
                        f"minimum {entry['min']:.6g}"
                    )
            achievable[name] = entry
    else:
        messages.append("no source water is available")

    if not messages:
        messages.append(
            "indicator ranges are individually attainable but jointly incompatible "
            "under the supply and volume constraints"
        )
    return SolveOutput(
        status="infeasible",
        achievable_ranges=achievable,
        message="; ".join(messages),
    )
