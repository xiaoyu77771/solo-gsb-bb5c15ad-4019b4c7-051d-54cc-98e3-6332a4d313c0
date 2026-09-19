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
