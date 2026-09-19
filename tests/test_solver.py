"""Unit tests for the LP solver (no HTTP layer)."""
import numpy as np
import pytest

from app.solver import SourceData, solve_blend, solve_contingency


def src(id, max_volume, cost, ca=50.0, mg=10.0, alk=100.0):
    return SourceData(
        id=id, max_volume=max_volume, cost=cost,
        calcium=ca, magnesium=mg, alkalinity=alk,
    )


def test_cheapest_source_wins():
    sources = [src("B", 1000, 2.0), src("A", 1000, 1.0)]
    out = solve_blend(sources, 500, {})
    assert out.status == "optimal"
    vols = dict(zip(["A", "B"], out.volumes))
    assert vols["A"] == pytest.approx(500.0, abs=1e-6)
    assert vols["B"] == pytest.approx(0.0, abs=1e-6)
    assert out.total_cost == pytest.approx(500.0, abs=1e-6)


def test_indicator_constraint_forces_mixing():
    sources = [
        src("W1", 600, 1.0, ca=100.0),
        src("W2", 800, 2.0, ca=20.0),
    ]
    out = solve_blend(sources, 1000, {"calcium": (40.0, 60.0)})
    assert out.status == "optimal"
    w1, w2 = out.volumes
    assert w1 == pytest.approx(500.0, abs=1e-6)
    assert w2 == pytest.approx(500.0, abs=1e-6)
    assert out.indicators["calcium"] == pytest.approx(60.0, abs=1e-6)
    assert out.total_cost == pytest.approx(1500.0, abs=1e-6)


def test_lexicographic_tie_break_uses_id_order():
    # Identical cost/concentrations: any split is optimal.  The usage vector
    # ordered by source id must be lexicographically minimal.
    sources = [
        src("C", 60, 1.0),
        src("A", 100, 1.0),
        src("B", 100, 1.0),
    ]
    out = solve_blend(sources, 100, {})
    assert out.status == "optimal"
    vols = dict(zip(["A", "B", "C"], out.volumes))
    assert vols["A"] == pytest.approx(0.0, abs=1e-6)
    assert vols["B"] == pytest.approx(40.0, abs=1e-6)
    assert vols["C"] == pytest.approx(60.0, abs=1e-6)


def test_infeasible_indicator_reports_achievable_range():
    sources = [
        src("W1", 600, 1.0, ca=100.0),
        src("W2", 800, 2.0, ca=20.0),
    ]
    out = solve_blend(sources, 1000, {"calcium": (70.0, 90.0)})
    assert out.status == "infeasible"
    ca = out.achievable_ranges["calcium"]
    assert ca["min"] == pytest.approx(36.0, abs=1e-6)
    assert ca["max"] == pytest.approx(68.0, abs=1e-6)
    assert ca["requested_min"] == 70.0
    assert "calcium" in out.message


def test_infeasible_volume_reports_max_available():
    sources = [src("W1", 600, 1.0), src("W2", 800, 2.0)]
    out = solve_blend(sources, 2000, {})
    assert out.status == "infeasible"
    assert out.achievable_ranges["volume"]["max"] == pytest.approx(1400.0)
    # ranges are computed against the actually attainable volume
    assert out.achievable_ranges["calcium"]["min"] >= 0


def test_jointly_incompatible_ranges():
    # Calcium wants lots of W1, alkalinity wants lots of W2; the windows
    # cannot be met at the same time.
    sources = [
        src("W1", 1000, 1.0, ca=100.0, alk=200.0),
        src("W2", 1000, 1.0, ca=0.0, alk=0.0),
    ]
    out = solve_blend(
        sources, 1000, {"calcium": (90.0, 100.0), "alkalinity": (0.0, 20.0)}
    )
    assert out.status == "infeasible"
    assert "jointly incompatible" in out.message


# --------------------------------------------------------------------------- #
# supply-cut contingency
# --------------------------------------------------------------------------- #

def test_contingency_keeps_original_when_cut_is_not_binding():
    sources = [src("A", 100, 1.0), src("B", 100, 2.0)]
    out = solve_contingency(sources, 100, {}, np.array([50.0, 50.0]), {"A": 80.0})
    assert out.status == "optimal"
    assert out.volumes == pytest.approx([50.0, 50.0], abs=1e-6)
    assert out.total_adjustment == pytest.approx(0.0, abs=1e-6)
    assert out.total_cost == pytest.approx(150.0, abs=1e-6)


def test_contingency_minimizes_adjustment_then_cost():
    # Original cheapest blend: A=500, C=500.  Cutting C to 400 forces
    # +100 elsewhere; cheapest replacement is A, so the unique minimum-change
    # blend is A=600, C=400.
    sources = [
        src("A", 600, 1.0),
        src("B", 800, 2.0),
        src("C", 600, 3.0),
    ]
    out = solve_contingency(sources, 1000, {}, np.array([500.0, 0.0, 500.0]),
                            {"C": 400.0})
    assert out.status == "optimal"
    assert out.volumes == pytest.approx([600.0, 0.0, 400.0], abs=1e-6)
    assert out.total_adjustment == pytest.approx(200.0, abs=1e-6)
    assert out.total_cost == pytest.approx(1800.0, abs=1e-6)


def test_contingency_cost_breaks_adjustment_ties():
    # Original A=400, B=0, C=600 is one minimum-cost blend (A is cheapest but
    # capped at 400).  Cutting C to 500 leaves exactly 100 to replace; both
    # A (400->500) and B (0->100) are equally costly in change, so the second
    # objective picks more of the cheaper source A.
    sources = [
        src("A", 500, 1.0),
        src("B", 500, 2.0),
        src("C", 500, 3.0),
    ]
    out = solve_contingency(sources, 1000, {}, np.array([400.0, 0.0, 600.0]),
                            {"C": 500.0})
    assert out.status == "optimal"
    assert out.volumes == pytest.approx([500.0, 0.0, 500.0], abs=1e-6)
    assert out.total_adjustment == pytest.approx(200.0, abs=1e-6)
    assert out.total_cost == pytest.approx(2000.0, abs=1e-6)


def test_contingency_lexicographic_usage_on_final_tie():
    # Identical unit costs: every blend has the same cost.  Cut A to 400
    # (original A=500, B=500, C=0).  Minimum adjustment is 200 (100 out of A,
    # 100 into B or C); (A,B,C)=(400,500,100) and (400,600,0) tie on every
    # objective, so the id-ascending lex rule minimizes B first.
    sources = [
        src("A", 1000, 1.0),
        src("B", 1000, 1.0),
        src("C", 1000, 1.0),
    ]
    x0 = np.array([500.0, 500.0, 0.0])
    out = solve_contingency(sources, 1000, {}, x0, {"A": 400.0})
    assert out.status == "optimal"
    assert out.volumes == pytest.approx([400.0, 500.0, 100.0], abs=1e-6)
    assert out.total_adjustment == pytest.approx(200.0, abs=1e-6)


def test_contingency_infeasible_when_volume_disappears():
    sources = [src("A", 600, 1.0), src("B", 400, 2.0)]
    out = solve_contingency(sources, 1000, {}, np.array([600.0, 400.0]),
                            {"A": 500.0})
    assert out.status == "infeasible"
    assert out.achievable_ranges["volume"]["max"] == pytest.approx(900.0)
