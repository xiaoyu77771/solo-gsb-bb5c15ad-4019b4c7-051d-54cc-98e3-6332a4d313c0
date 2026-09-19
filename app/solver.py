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

``solve_contingency`` handles a supply-cut on a previously saved optimal
blend: with deviation variables d_i (|x_i - x0_i| <= d_i) it lexicographically
minimizes (sum d_i, total cost, x_1, ..., x_n) under the reduced caps, keeping
the target volume, indicator ranges and costs unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.optimize import linprog

INDICATORS: Tuple[str, ...] = ("calcium", "magnesium", "alkalinity")

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
    total_adjustment: Optional[float] = None  # min sum |x_i - x0_i| (contingency)


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


def solve_contingency(
    sources: List[SourceData],
    target_volume: float,
    limits: Dict[str, Tuple[float, float]],
    original_volumes: np.ndarray,
    new_caps: Dict[str, float],
) -> SolveOutput:
    """Re-blend after a supply-cut contingency.

    Unchanged target volume, indicator ranges and costs; the affected source
    caps are replaced by ``new_caps`` (already validated against the originals).

    Hierarchical objective:
      1. minimize the total adjustment ``sum_i |x_i - x0_i|``;
      2. among minimum-adjustment blends, minimize total cost;
      3. lexicographically minimize the id-ordered usage vector on final ties.

    Deviations are linearized as ``x_i - x0_i <= d_i`` and ``x0_i - x_i <= d_i``
    with ``d_i >= 0``.
    """
    sources = sorted(sources, key=lambda s: s.id)
    n = len(sources)
    costs = np.array([s.cost for s in sources], dtype=float)
    x0 = np.asarray(original_volumes, dtype=float)
    cap_by_id = {s.id: float(new_caps.get(s.id, s.max_volume)) for s in sources}
    x_bounds = [(0.0, cap_by_id[s.id]) for s in sources]

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

    # Deviation variables d_i appended after the x_i variables.
    def with_devs(mat: Optional[np.ndarray]) -> np.ndarray:
        if mat is None:
            return np.zeros((0, 2 * n))
        return np.hstack([mat, np.zeros((mat.shape[0], n))])

    A_ub = with_devs(np.asarray(rows) if rows else None)
    b_ub = np.asarray(rhs) if rows else np.zeros(0)
    eye = np.eye(n)
    dev_rows = np.vstack([
        np.hstack([eye, -eye]),    # x_i - x0_i <= d_i
        np.hstack([-eye, -eye]),   # x0_i - x_i <= d_i
    ])
    A_ub = np.vstack([A_ub, dev_rows])
    b_ub = np.concatenate([b_ub, x0, -x0])
    A_eq_ext = np.hstack([A_eq, np.zeros((1, n))])
    bounds = x_bounds + [(0.0, None)] * n

    # Stage 1: feasibility check + minimum total adjustment.
    res = linprog(
        np.concatenate([np.zeros(n), np.ones(n)]),
        A_ub=A_ub, b_ub=b_ub, A_eq=A_eq_ext, b_eq=b_eq,
        bounds=bounds, method="highs",
    )
    if res.status == 2:  # provably infeasible
        return _infeasible(sources, target_volume, limits, x_bounds)
    if res.status != 0:
        raise RuntimeError(f"LP solver failed (status {res.status}): {res.message}")

    d_star = float(res.x[n:].sum())
    pin_adj = np.concatenate([np.zeros(n), np.ones(n)]).reshape(1, -1)

    # Stages 2/3 pin the earlier optima as equalities so later lexicographic
    # objectives cannot drift into a pin's slack; fall back to tight
    # inequalities if an exact pin proves numerically unsatisfiable.
    try:
        # Stage 2: keep the minimum adjustment, minimize total cost.
        A_eq2 = np.vstack([A_eq_ext, pin_adj])
        b_eq2 = np.append(b_eq, d_star)
        res2 = linprog(
            np.concatenate([costs, np.zeros(n)]),
            A_ub=A_ub, b_ub=b_ub, A_eq=A_eq2, b_eq=b_eq2,
            bounds=bounds, method="highs",
        )
        if res2.status != 0:
            raise RuntimeError(f"contingency cost refinement failed: {res2.message}")
        cost_star = float(costs @ res2.x[:n])

        # Stage 3: pin cost as well, lexicographically minimize the usage
        # vector (only the x_i variables; d_i stay free beyond index n-1).
        pin_cost = np.concatenate([costs, np.zeros(n)]).reshape(1, -1)
        A_eq3 = np.vstack([A_eq2, pin_cost])
        b_eq3 = np.append(b_eq2, cost_star)
        x_full = _sequential_min(2 * n, A_ub, b_ub, A_eq3, b_eq3, bounds, count=n)
    except RuntimeError:
        tol_adj = _COST_RTOL * max(1.0, abs(d_star))
        A_ub2 = np.vstack([A_ub, pin_adj])
        b_ub2 = np.append(b_ub, d_star + tol_adj)
        res2 = linprog(
            np.concatenate([costs, np.zeros(n)]),
            A_ub=A_ub2, b_ub=b_ub2, A_eq=A_eq_ext, b_eq=b_eq,
            bounds=bounds, method="highs",
        )
        if res2.status != 0:  # pragma: no cover - defensive
            raise RuntimeError(f"contingency cost refinement failed: {res2.message}")
        cost_star = float(costs @ res2.x[:n])
        tol_cost = _COST_RTOL * max(1.0, abs(cost_star))
        pin_cost = np.concatenate([costs, np.zeros(n)]).reshape(1, -1)
        A_ub3 = np.vstack([A_ub2, pin_cost])
        b_ub3 = np.append(b_ub2, cost_star + tol_cost)
        x_full = _sequential_min(2 * n, A_ub3, b_ub3, A_eq_ext, b_eq, bounds, count=n)
    x = x_full[:n]

    indicators = {
        name: float(np.array([getattr(s, name) for s in sources]) @ x) / target_volume
        for name in INDICATORS
    }
    return SolveOutput(
        status="optimal",
        volumes=x,
        total_cost=float(costs @ x),
        indicators=indicators,
        total_adjustment=float(np.abs(x - x0).sum()),
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


def _sequential_min(n, A_ub, b_ub, A_eq, b_eq, bounds, count: Optional[int] = None) -> np.ndarray:
    """Minimize (x_1, ..., x_count) lexicographically (default: all vars).

    Indices follow the id-sorted source order.  Variables beyond ``count`` are
    left free.  Each previously minimized variable is pinned to its found
    value with an exact equality row (the original bounds stay untouched),
    avoiding the compounded slack of shrinking several intervals."""
    count = n if count is None else min(count, n)
    fix_rows, fix_rhs = [], []
    x = np.zeros(n)
    for k in range(count):
        Aeq = A_eq if not fix_rows else np.vstack(
            [A_eq, np.asarray(fix_rows).reshape(-1, n)]
        )
        beq = b_eq if not fix_rhs else np.append(b_eq, fix_rhs)
        obj = np.zeros(n)
        obj[k] = 1.0
        r = linprog(obj, A_ub=A_ub, b_ub=b_ub, A_eq=Aeq, b_eq=beq,
                    bounds=bounds, method="highs")
        if r.status != 0:
            raise RuntimeError(f"lexicographic refinement failed at index {k}: {r.message}")
        lo0, hi0 = bounds[k]
        v = min(max(float(r.x[k]), lo0), hi0 if hi0 is not None else float(r.x[k]))
        x[k] = v
        row = np.zeros(n)
        row[k] = 1.0
        fix_rows.append(row)
        fix_rhs.append(v)
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
