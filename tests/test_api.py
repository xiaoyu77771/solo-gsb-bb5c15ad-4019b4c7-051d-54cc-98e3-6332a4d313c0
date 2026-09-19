"""End-to-end API tests."""
import pytest

from tests.conftest import make_request


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_solve_optimal(client):
    r = client.post("/api/v1/blends/solve", json=make_request())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "optimal"
    assert body["plan_id"] is None
    assert body["total_cost"] == pytest.approx(1500.0, abs=1e-6)
    blend = {a["source_id"]: a["volume"] for a in body["blend"]}
    assert blend["W1"] == pytest.approx(500.0, abs=1e-6)
    assert blend["W2"] == pytest.approx(500.0, abs=1e-6)
    assert body["indicators"]["calcium"] == pytest.approx(60.0, abs=1e-6)
    assert body["indicators"]["magnesium"] == pytest.approx(17.5, abs=1e-6)
    assert body["indicators"]["alkalinity"] == pytest.approx(165.0, abs=1e-6)


def test_solve_tie_break_lexicographic(client):
    payload = {
        "target_volume": 100,
        "sources": [
            {"id": "A", "max_volume": 100, "cost": 1, "calcium": 50,
             "magnesium": 10, "alkalinity": 100},
            {"id": "B", "max_volume": 100, "cost": 1, "calcium": 50,
             "magnesium": 10, "alkalinity": 100},
            {"id": "C", "max_volume": 60, "cost": 1, "calcium": 50,
             "magnesium": 10, "alkalinity": 100},
        ],
    }
    r = client.post("/api/v1/blends/solve", json=payload)
    assert r.status_code == 200
    blend = {a["source_id"]: a["volume"] for a in r.json()["blend"]}
    assert blend["A"] == pytest.approx(0.0, abs=1e-6)
    assert blend["B"] == pytest.approx(40.0, abs=1e-6)
    assert blend["C"] == pytest.approx(60.0, abs=1e-6)


def test_solve_infeasible_indicator(client):
    r = client.post(
        "/api/v1/blends/solve", json=make_request(calcium={"min": 70, "max": 90})
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible"
    ca = body["achievable_ranges"]["calcium"]
    assert ca["max"] == pytest.approx(68.0, abs=1e-6)
    assert ca["requested_min"] == 70
    assert "calcium" in body["message"]


def test_solve_infeasible_volume(client):
    r = client.post("/api/v1/blends/solve", json=make_request(target_volume=2000))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["achievable_ranges"]["volume"]["max"] == pytest.approx(1400.0)


def test_create_and_get_plan(client):
    r = client.post("/api/v1/blends", json=make_request(idempotency_key="plan-1"))
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "optimal"
    assert body["plan_id"] is not None
    assert body["created_at"]

    g = client.get(f"/api/v1/blends/{body['plan_id']}")
    assert g.status_code == 200
    assert g.json() == body


def test_get_plan_not_found(client):
    assert client.get("/api/v1/blends/9999").status_code == 404


def test_idempotent_replay(client):
    payload = make_request(idempotency_key="replay-me")
    r1 = client.post("/api/v1/blends", json=payload)
    r2 = client.post("/api/v1/blends", json=payload)
    assert r1.status_code == 201
    assert r2.status_code == 200
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json() == r2.json()


def test_idempotency_conflict(client):
    client.post("/api/v1/blends", json=make_request(idempotency_key="dup"))
    r = client.post(
        "/api/v1/blends",
        json=make_request(idempotency_key="dup", target_volume=500),
    )
    assert r.status_code == 409


def test_idempotency_key_via_header(client):
    r = client.post(
        "/api/v1/blends",
        json=make_request(),
        headers={"Idempotency-Key": "header-key"},
    )
    assert r.status_code == 201
    again = client.post(
        "/api/v1/blends",
        json=make_request(),
        headers={"Idempotency-Key": "header-key"},
    )
    assert again.status_code == 200
    assert again.json()["plan_id"] == r.json()["plan_id"]


def test_missing_idempotency_key(client):
    r = client.post("/api/v1/blends", json=make_request())
    assert r.status_code == 422


def test_infeasible_plan_is_persisted(client):
    r = client.post(
        "/api/v1/blends",
        json=make_request(idempotency_key="infeasible", calcium={"min": 70, "max": 90}),
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "infeasible"
    g = client.get(f"/api/v1/blends/{body['plan_id']}")
    assert g.status_code == 200
    assert g.json()["status"] == "infeasible"
    assert "calcium" in g.json()["achievable_ranges"]


def test_validation_source_count(client):
    one = make_request()
    one["sources"] = one["sources"][:1]
    assert client.post("/api/v1/blends/solve", json=one).status_code == 422

    seven = make_request()
    seven["sources"] = [
        {"id": f"S{i}", "max_volume": 10, "cost": 1, "calcium": 1,
         "magnesium": 1, "alkalinity": 1}
        for i in range(7)
    ]
    assert client.post("/api/v1/blends/solve", json=seven).status_code == 422


def test_validation_bad_range_and_duplicate_ids(client):
    bad_range = make_request(calcium={"min": 60, "max": 40})
    assert client.post("/api/v1/blends/solve", json=bad_range).status_code == 422

    dup = make_request()
    dup["sources"][1]["id"] = "W1"
    assert client.post("/api/v1/blends/solve", json=dup).status_code == 422

    neg = make_request(target_volume=-5)
    assert client.post("/api/v1/blends/solve", json=neg).status_code == 422


# --------------------------------------------------------------------------- #
# supply-cut contingency
# --------------------------------------------------------------------------- #

def _save_plan(client, key="cont-plan", **overrides):
    r = client.post("/api/v1/blends", json=make_request(idempotency_key=key, **overrides))
    assert r.status_code == 201
    return r.json()


def test_contingency_reblends_optimal_plan(client):
    plan = _save_plan(client)
    assert [(a["source_id"], a["volume"]) for a in plan["blend"]] == [
        ("W1", 500.0), ("W2", 500.0)
    ]
    r = client.post(
        f"/api/v1/blends/{plan['plan_id']}/contingency",
        json={"reductions": {"W1": 400}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "optimal"
    assert body["plan_id"] == plan["plan_id"]
    assert body["total_volume"] == 1000.0
    assert body["reductions"] == {"W1": 400.0}

    by_id = {a["source_id"]: a for a in body["blend"]}
    assert by_id["W1"]["volume"] == pytest.approx(400.0, abs=1e-6)
    assert by_id["W2"]["volume"] == pytest.approx(600.0, abs=1e-6)
    assert by_id["W1"]["volume_delta"] == pytest.approx(-100.0, abs=1e-6)
    assert by_id["W2"]["volume_delta"] == pytest.approx(100.0, abs=1e-6)
    assert by_id["W1"]["original_volume"] == 500.0
    assert by_id["W1"]["original_max_volume"] == 600.0
    assert by_id["W1"]["effective_max_volume"] == 400.0
    assert by_id["W2"]["effective_max_volume"] == 800.0
    assert body["total_adjustment"] == pytest.approx(200.0, abs=1e-6)
    assert body["original_total_cost"] == pytest.approx(1500.0, abs=1e-6)
    assert body["total_cost"] == pytest.approx(1600.0, abs=1e-6)
    assert body["cost_delta"] == pytest.approx(100.0, abs=1e-6)
    assert body["indicators"]["calcium"] == pytest.approx(52.0, abs=1e-6)


def test_contingency_zero_change_when_cut_is_above_use(client):
    plan = _save_plan(client, key="cont-zero")
    r = client.post(
        f"/api/v1/blends/{plan['plan_id']}/contingency",
        json={"reductions": {"W1": 500}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_adjustment"] == pytest.approx(0.0, abs=1e-6)
    assert body["cost_delta"] == pytest.approx(0.0, abs=1e-6)
    assert {a["source_id"]: a["volume"] for a in body["blend"]} == {
        "W1": 500.0, "W2": 500.0
    }


def test_contingency_does_not_modify_or_save(client):
    plan = _save_plan(client, key="cont-readonly")
    client.post(
        f"/api/v1/blends/{plan['plan_id']}/contingency",
        json={"reductions": {"W1": 400}},
    )
    stored = client.get(f"/api/v1/blends/{plan['plan_id']}").json()
    assert stored == plan
    # no extra rows persisted either
    assert client.get("/api/v1/blends/9999").status_code == 404


def test_contingency_infeasible_returns_reason(client):
    plan = _save_plan(client, key="cont-inf")
    r = client.post(
        f"/api/v1/blends/{plan['plan_id']}/contingency",
        json={"reductions": {"W1": 100}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "infeasible"
    assert body["plan_id"] == plan["plan_id"]
    assert body["reductions"] == {"W1": 100.0}
    assert body["achievable_ranges"]["volume"]["max"] == pytest.approx(900.0)
    assert "below the target" in body["message"]
    # the original optimal plan is still intact
    stored = client.get(f"/api/v1/blends/{plan['plan_id']}").json()
    assert stored["status"] == "optimal"


def test_contingency_unknown_plan_is_404(client):
    r = client.post("/api/v1/blends/9999/contingency", json={"reductions": {"W1": 1}})
    assert r.status_code == 404


def test_contingency_non_optimal_plan_is_422(client):
    r = client.post(
        "/api/v1/blends",
        json=make_request(idempotency_key="cont-bad-plan", calcium={"min": 70, "max": 90}),
    )
    assert r.status_code == 201
    pid = r.json()["plan_id"]
    resp = client.post(f"/api/v1/blends/{pid}/contingency", json={"reductions": {"W1": 1}})
    assert resp.status_code == 422
    assert "optimal" in resp.json()["detail"]


def test_contingency_validation_errors_are_422(client):
    plan = _save_plan(client, key="cont-valid")
    base = f"/api/v1/blends/{plan['plan_id']}/contingency"

    assert client.post(base, json={"reductions": {}}).status_code == 422
    assert client.post(base, json={"reductions": {"NOPE": 10}}).status_code == 422
    assert client.post(base, json={"reductions": {"W1": -0.001}}).status_code == 422
    # above the original supply cap (600 for W1), not just above current use
    assert client.post(base, json={"reductions": {"W1": 601}}).status_code == 422
    # cap exactly at the original maximum is legal (no change in that cap)
    ok = client.post(base, json={"reductions": {"W1": 600}})
    assert ok.status_code == 200
    assert client.post(base, json={"reductions": {"W1": "x"}}).status_code == 422
