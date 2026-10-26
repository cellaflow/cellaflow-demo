"""Runs the demo against a real engine (ENGINE_TARGET, default localhost:50051).

See the README for the command that runs this on the compose network.
"""

import os
import time

os.environ.setdefault("DEMO_PRICE_SECONDS", "0.2")
os.environ.setdefault("DEMO_ORDER_SECONDS", "1.5")
os.environ.setdefault("DEMO_CONFIRM_SECONDS", "0.5")
os.environ.setdefault("DEMO_RESTART_DELAY_SECONDS", "0.5")
os.environ.setdefault("DEMO_RUNS_PER_IP_PER_MINUTE", "100")
os.environ.setdefault("DEMO_MAX_ACTIVE_RUNS", "100")

from fastapi.testclient import TestClient  # noqa: E402

import app as demo  # noqa: E402

client = TestClient(demo.app)


def snapshot(run_id: str) -> dict:
    return client.get(f"/api/runs/{run_id}").json()


def wait_until(run_id: str, condition, timeout: float = 60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = snapshot(run_id)
        if condition(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out; last state: {s}")


def all_done(s: dict) -> bool:
    return all(a["state"] in ("done", "error") for arm in s["arms"].values() for a in arm["agents"])


def cellaflow(s: dict) -> dict:
    return s["arms"][demo.CELLAFLOW]


def naive(s: dict) -> dict:
    return s["arms"][demo.NAIVE]


def assert_one_order_one_confirmation(s: dict) -> None:
    arm = cellaflow(s)
    assert len(arm["orders"]) == 1
    assert len(arm["confirmations"]) == 1
    assert {a["confirmation"] for a in arm["agents"]} == {"ORD-4417-1"}
    assert all(a["state"] == "done" for a in arm["agents"])


def test_agents_started_together_place_one_order_and_send_one_confirmation():
    run = client.post("/api/runs", json={"agents": 3}).json()
    s = wait_until(run["id"], all_done)

    assert_one_order_one_confirmation(s)
    # The same agents, checking before acting, each placed and confirmed their own.
    assert len(naive(s)["orders"]) == 3
    assert len(naive(s)["confirmations"]) == 3


def test_a_killed_agent_recovers_its_recorded_steps_from_the_engine():
    run = client.post("/api/runs", json={"agents": 2}).json()

    def order_recorded(s):
        return next(
            (a for a in cellaflow(s)["agents"]
             if a["state"] == "running" and a["steps"]["order"] in ("done", "received")),
            None,
        )

    target = order_recorded(wait_until(run["id"], order_recorded))
    response = client.post(f"/api/runs/{run['id']}/agents/{target['number']}/kill")
    assert response.status_code == 200
    s = wait_until(run["id"], all_done)

    assert_one_order_one_confirmation(s)
    killed = cellaflow(s)["agents"][target["number"] - 1]
    assert killed["kills"] == 1 and killed["attempt"] == 2
    # Its own earlier work comes back as recovered; another agent's as received. Nothing re-ran.
    assert killed["steps"]["price"] == "recovered"
    assert killed["steps"]["order"] in ("recovered", "received")


def test_killing_the_agent_holding_the_order_lease_still_places_one_order():
    run = client.post("/api/runs", json={"agents": 2}).json()

    def holder(s):
        return next((a for a in cellaflow(s)["agents"] if a["steps"]["order"] == "running"), None)

    target = holder(wait_until(run["id"], holder))
    s = client.post(f"/api/runs/{run['id']}/agents/{target['number']}/kill").json()
    # The page counts down to when the dead holder's lease lapses.
    assert s["lease_lapses_ms"]["order"] > s["now_ms"]

    # Up to the engine's 20 s lease, then one agent places the order.
    s = wait_until(run["id"], all_done, timeout=90)
    assert_one_order_one_confirmation(s)
    assert s["lease_lapses_ms"] == {}


def test_a_kill_needs_a_running_agent_and_a_run_caps_its_agents():
    run = client.post("/api/runs", json={"agents": demo.MAX_AGENTS_PER_RUN}).json()
    assert client.post(f"/api/runs/{run['id']}/agents", json={"agents": 1}).status_code == 400
    wait_until(run["id"], all_done)
    assert client.post(f"/api/runs/{run['id']}/agents/1/kill").status_code == 409
    assert client.post(f"/api/runs/{run['id']}/agents/99/kill").status_code == 404
