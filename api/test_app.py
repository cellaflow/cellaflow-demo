"""Runs the demo against a real engine (ENGINE_TARGET, default localhost:50051).

    docker compose up -d engine   # then publish 50051, or run inside the compose network
    DEMO_ORDER_SECONDS=2 pytest api
"""

import os
import time

os.environ.setdefault("DEMO_ORDER_SECONDS", "2")

from fastapi.testclient import TestClient  # noqa: E402

import app as demo  # noqa: E402

client = TestClient(demo.app)


def wait_until_finished(run_id: str, timeout: float = 30) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = client.get(f"/api/runs/{run_id}").json()
        agents = [a for arm in snapshot["arms"].values() for a in arm["agents"]]
        if all(a["finished_ms"] is not None for a in agents):
            return snapshot
        time.sleep(0.2)
    raise AssertionError("agents did not finish")


def test_agents_started_together_and_mid_flight_place_one_order_with_cellaflow():
    run = client.post("/api/runs", json={"agents": 3}).json()
    time.sleep(demo.ORDER_SECONDS / 2)
    client.post(f"/api/runs/{run['id']}/agents", json={"agents": 2})
    snapshot = wait_until_finished(run["id"])

    with_cf = snapshot["arms"][demo.CELLAFLOW]
    assert len(with_cf["orders"]) == 1
    confirmation = with_cf["orders"][0]["confirmation"]
    # Every agent, including the two added mid-flight, holds the one order's confirmation.
    assert {a["confirmation"] for a in with_cf["agents"]} == {confirmation}
    assert sorted(a["state"] for a in with_cf["agents"]) == ["placed"] + ["received"] * 4

    # The same five agents, checking before acting, each placed their own.
    assert len(snapshot["arms"][demo.NAIVE]["orders"]) == 5


def test_an_agent_arriving_after_the_order_gets_the_stored_result():
    run = client.post("/api/runs", json={"agents": 1}).json()
    wait_until_finished(run["id"])
    client.post(f"/api/runs/{run['id']}/agents", json={"agents": 1})
    snapshot = wait_until_finished(run["id"])

    late = snapshot["arms"][demo.CELLAFLOW]["agents"][1]
    assert late["state"] == "received"
    assert late["placed_by"] == 1
    assert snapshot["arms"][demo.NAIVE]["agents"][1]["state"] == "saw_order"


def test_a_run_refuses_more_agents_than_its_cap():
    run = client.post("/api/runs", json={"agents": demo.MAX_AGENTS_PER_RUN}).json()
    response = client.post(f"/api/runs/{run['id']}/agents", json={"agents": 1})
    assert response.status_code == 400
