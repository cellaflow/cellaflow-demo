"""The live demo behind cellaflow.com: several agents race to place one order.

Every agent in a run is started twice, at the same moment, in two arms:

- **without Cellaflow**, the agent checks whether the order exists and, if not, places it. This is
  the check-then-act guard agents usually write for themselves.
- **with Cellaflow**, the agent calls a `SCOPE_SHARED` tool through the real Python SDK against a
  real engine. The engine leases the call to one agent; the others wait on the lease and are handed
  that agent's result.

Each arm has its own ledger, standing in for the shop. The counters the page shows are read from the
ledgers, never from what the agents report, so a duplicate cannot hide behind an agent saying it
succeeded.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from cellaflow import IdempotencyScope, tool, workflow
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

ENGINE_TARGET = os.environ.get("ENGINE_TARGET", "localhost:50051")
# How long placing the order takes. Long enough for a visitor to add agents while it is in flight.
ORDER_SECONDS = float(os.environ.get("DEMO_ORDER_SECONDS", "6"))
MAX_AGENTS_PER_RUN = int(os.environ.get("DEMO_MAX_AGENTS_PER_RUN", "10"))
MAX_ACTIVE_RUNS = int(os.environ.get("DEMO_MAX_ACTIVE_RUNS", "20"))
RUN_TTL_SECONDS = int(os.environ.get("DEMO_RUN_TTL_SECONDS", "120"))
RUNS_PER_IP_PER_MINUTE = int(os.environ.get("DEMO_RUNS_PER_IP_PER_MINUTE", "6"))
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "DEMO_ALLOWED_ORIGINS", "https://cellaflow.com,https://www.cellaflow.com,http://localhost:3000"
    ).split(",")
    if o.strip()
]

ORDER_ID = "4417"
NAIVE = "without_cellaflow"
CELLAFLOW = "with_cellaflow"
ARMS = (NAIVE, CELLAFLOW)

log = logging.getLogger("cellaflow_demo")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Agent:
    number: int
    state: str = "starting"
    started_ms: int = field(default_factory=now_ms)
    finished_ms: Optional[int] = None
    confirmation: Optional[str] = None
    placed_by: Optional[int] = None
    error: Optional[str] = None

    def snapshot(self) -> dict:
        return {
            "number": self.number,
            "state": self.state,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "confirmation": self.confirmation,
            "placed_by": self.placed_by,
            "error": self.error,
        }


class Ledger:
    """The shop's record of orders actually placed. The only source the counters are read from."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._orders: list[dict] = []

    def has_order(self) -> bool:
        with self._lock:
            return bool(self._orders)

    def place(self, agent_number: int) -> str:
        with self._lock:
            confirmation = f"ORD-{ORDER_ID}-{len(self._orders) + 1}"
            self._orders.append({"confirmation": confirmation, "agent": agent_number, "at_ms": now_ms()})
            return confirmation

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._orders)


@dataclass
class Run:
    id: str
    created_ms: int = field(default_factory=now_ms)
    lock: threading.Lock = field(default_factory=threading.Lock)
    agents: dict = field(default_factory=lambda: {arm: [] for arm in ARMS})
    ledgers: dict = field(default_factory=lambda: {arm: Ledger() for arm in ARMS})

    @property
    def coordination_id(self) -> str:
        # Names the shared work for SCOPE_SHARED. Unique per run, so visitors never share a key.
        return f"demo-{self.id}"

    def expired(self) -> bool:
        return now_ms() - self.created_ms > RUN_TTL_SECONDS * 1000

    def agent(self, arm: str, number: int) -> Agent:
        return self.agents[arm][number - 1]

    def snapshot(self) -> dict:
        with self.lock:
            arms = {
                arm: {
                    "orders": self.ledgers[arm].snapshot(),
                    "agents": [a.snapshot() for a in self.agents[arm]],
                }
                for arm in ARMS
            }
        return {
            "id": self.id,
            "order_id": ORDER_ID,
            "order_seconds": ORDER_SECONDS,
            "created_ms": self.created_ms,
            "now_ms": now_ms(),
            "expires_ms": self.created_ms + RUN_TTL_SECONDS * 1000,
            "max_agents": MAX_AGENTS_PER_RUN,
            "arms": arms,
        }


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()


def find_run(run_id: str) -> Run:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run. Runs expire after two minutes; start a new one.")
    return run


# --- Arm 1: without Cellaflow ---------------------------------------------------------------------


def naive_agent(run: Run, number: int) -> None:
    agent = run.agent(NAIVE, number)
    ledger = run.ledgers[NAIVE]
    agent.state = "checking"
    if ledger.has_order():
        # An agent that arrives after the order exists sees it and stands down.
        agent.state = "saw_order"
    else:
        agent.state = "placing"
        time.sleep(ORDER_SECONDS)
        agent.confirmation = ledger.place(number)
        agent.placed_by = number
        agent.state = "placed"
    agent.finished_ms = now_ms()


# --- Arm 2: with Cellaflow ------------------------------------------------------------------------


@tool(tool_name="place_order", scope=IdempotencyScope.SCOPE_SHARED, shared_on=["order_id"])
def place_order(order_id: str, run_id: str, agent_number: int) -> dict:
    # The body runs only for the agent holding the engine's lease. Everyone else gets its return
    # value from the engine without this code running.
    run = find_run(run_id)
    agent = run.agent(CELLAFLOW, agent_number)
    agent.state = "placing"
    time.sleep(ORDER_SECONDS)
    confirmation = run.ledgers[CELLAFLOW].place(agent_number)
    return {"confirmation": confirmation, "placed_by": agent_number}


@workflow(version="1.0.0", target=ENGINE_TARGET)
def order_agent(order_id: str, run_id: str, agent_number: int) -> dict:
    return place_order(order_id, run_id, agent_number)


def cellaflow_agent(run: Run, number: int) -> None:
    agent = run.agent(CELLAFLOW, number)
    agent.state = "calling"
    try:
        result = order_agent(ORDER_ID, run.id, number, _coordination_id=run.coordination_id)
        agent.confirmation = result["confirmation"]
        agent.placed_by = result["placed_by"]
        agent.state = "placed" if result["placed_by"] == number else "received"
    except Exception as exc:  # shown on the page rather than hidden
        log.exception("cellaflow agent failed")
        agent.state = "error"
        agent.error = type(exc).__name__
    agent.finished_ms = now_ms()


class WaitingDetector(logging.Handler):
    """Marks an agent as waiting when the SDK reports the engine has the step in progress.

    The SDK logs that line each time the engine answers IN_PROGRESS, from the agent's own thread,
    so the thread name identifies the agent. This reads the engine's answer; nothing is inferred.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if "in progress by another worker" not in record.getMessage():
            return
        try:
            run_id, arm, number = record.threadName.split("|")
            agent = find_run(run_id).agent(arm, int(number))
        except (ValueError, HTTPException, IndexError):
            return
        if agent.state == "calling":
            agent.state = "waiting"


sdk_logger = logging.getLogger("cellaflow.decorators")
sdk_logger.setLevel(logging.INFO)
sdk_logger.addHandler(WaitingDetector())


def launch(run: Run, count: int) -> None:
    """Starts `count` new agents in both arms at the same moment."""
    threads = []
    with run.lock:
        # Checked under the run's lock, so two concurrent requests cannot both pass it.
        if len(run.agents[NAIVE]) + count > MAX_AGENTS_PER_RUN:
            raise HTTPException(400, f"A run holds at most {MAX_AGENTS_PER_RUN} agents.")
        for _ in range(count):
            number = len(run.agents[NAIVE]) + 1
            for arm, target in ((NAIVE, naive_agent), (CELLAFLOW, cellaflow_agent)):
                run.agents[arm].append(Agent(number=number))
                threads.append(
                    threading.Thread(target=target, args=(run, number), name=f"{run.id}|{arm}|{number}", daemon=True)
                )
    for thread in threads:
        thread.start()


# --- Limits ---------------------------------------------------------------------------------------

RECENT_RUNS_BY_IP: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Caddy, the only thing in front of this, sets X-Forwarded-For to the client's address.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def admit_run(ip: str) -> None:
    cutoff = time.time() - 60
    with RUNS_LOCK:
        recent = RECENT_RUNS_BY_IP[ip]
        while recent and recent[0] < cutoff:
            recent.popleft()
        if len(recent) >= RUNS_PER_IP_PER_MINUTE:
            raise HTTPException(429, "Too many runs from this address. Try again in a minute.")
        active = sum(1 for r in RUNS.values() if not r.expired())
        if active >= MAX_ACTIVE_RUNS:
            raise HTTPException(503, "The demo is busy. Try again in a minute.")
        recent.append(time.time())


def reap_expired_runs() -> None:
    while True:
        time.sleep(30)
        with RUNS_LOCK:
            for run_id in [rid for rid, run in RUNS.items() if run.expired()]:
                # Keep a run a little past expiry so a page polling it sees the final state.
                if now_ms() - RUNS[run_id].created_ms > (RUN_TTL_SECONDS + 60) * 1000:
                    del RUNS[run_id]
            for ip in [ip for ip, q in RECENT_RUNS_BY_IP.items() if not q or q[-1] < time.time() - 60]:
                del RECENT_RUNS_BY_IP[ip]


threading.Thread(target=reap_expired_runs, name="reaper", daemon=True).start()


# --- HTTP -----------------------------------------------------------------------------------------

app = FastAPI(title="Cellaflow demo", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


class AgentsRequest(BaseModel):
    agents: int = Field(ge=1, le=MAX_AGENTS_PER_RUN)


@app.get("/api/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/api/runs")
def start_run(body: AgentsRequest, request: Request) -> dict:
    admit_run(client_ip(request))
    run = Run(id=uuid.uuid4().hex[:12] + secrets.token_hex(2))
    with RUNS_LOCK:
        RUNS[run.id] = run
    launch(run, body.agents)
    return run.snapshot()


@app.post("/api/runs/{run_id}/agents")
def add_agents(run_id: str, body: AgentsRequest) -> dict:
    run = find_run(run_id)
    if run.expired():
        raise HTTPException(410, "This run has ended. Start a new one.")
    launch(run, body.agents)
    return run.snapshot()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return find_run(run_id).snapshot()
