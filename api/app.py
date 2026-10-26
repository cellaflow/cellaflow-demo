"""The live demo behind cellaflow.com: several agents place one order, and you can kill them.

Every agent in a run is started twice, at the same moment, in two arms (see ``agents.py``):
without Cellaflow, where an agent keeps its progress in memory, and with Cellaflow, where each
agent is a real Python SDK client in its own OS process, talking to a real engine.

Killing an agent sends SIGKILL to its process on the Cellaflow side and stops it at the same
moment on the other. Both are restarted two seconds later: the Cellaflow agent on its own
session, where the SDK recovers every step the engine recorded, and the other from step 1.

The counters the page shows are read from the shop's and the outbox's files, never from what
the agents report.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import secrets
import shutil
import signal
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import agents

ENGINE_TARGET = os.environ.get("ENGINE_TARGET", "localhost:50051")
MAX_AGENTS_PER_RUN = int(os.environ.get("DEMO_MAX_AGENTS_PER_RUN", "8"))
MAX_ACTIVE_RUNS = int(os.environ.get("DEMO_MAX_ACTIVE_RUNS", "6"))
MAX_KILLS_PER_RUN = int(os.environ.get("DEMO_MAX_KILLS_PER_RUN", "12"))
RUN_TTL_SECONDS = int(os.environ.get("DEMO_RUN_TTL_SECONDS", "180"))
RUNS_PER_IP_PER_MINUTE = int(os.environ.get("DEMO_RUNS_PER_IP_PER_MINUTE", "6"))
RESTART_DELAY_SECONDS = float(os.environ.get("DEMO_RESTART_DELAY_SECONDS", "2"))
#: An agent process still running after this long is stopped, so nothing outlives its run.
AGENT_DEADLINE_SECONDS = float(os.environ.get("DEMO_AGENT_DEADLINE_SECONDS", "120"))
#: The engine's default lease, which the SDK's decorators use. A lease whose holder was killed
#: lapses within this long of its last heartbeat; the page counts down from the kill.
LEASE_TTL_MS = 20_000
DATA_DIR = Path(os.environ.get("DEMO_DATA_DIR", "/tmp/cellaflow-demo"))
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "DEMO_ALLOWED_ORIGINS", "https://cellaflow.com,https://www.cellaflow.com,http://localhost:3000"
    ).split(",")
    if o.strip()
]

NAIVE = "without_cellaflow"
CELLAFLOW = "with_cellaflow"
ARMS = (NAIVE, CELLAFLOW)

log = logging.getLogger("cellaflow_demo")

# Agent processes come from a fork server with the SDK already imported, so starting one takes
# milliseconds and its imported code is shared rather than copied.
CTX = mp.get_context("forkserver")
CTX.set_forkserver_preload(["agents", "cellaflow", "grpc"])


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Agent:
    number: int
    arm: str
    state: str = "running"  # running | killed | restarting | done | error
    attempt: int = 0
    kills: int = 0
    steps: Dict[str, str] = field(default_factory=lambda: {s: "pending" for s in agents.STEPS})
    #: For a step whose result came from the engine, which agent produced it.
    step_by: Dict[str, int] = field(default_factory=dict)
    outcome: Optional[str] = None
    confirmation: Optional[str] = None
    placed_by: Optional[int] = None
    started_ms: int = field(default_factory=now_ms)
    killed_ms: Optional[int] = None
    finished_ms: Optional[int] = None
    kill_requested: bool = False
    pid: Optional[int] = None
    stop: Optional[threading.Event] = None

    def snapshot(self) -> dict:
        return {
            "number": self.number,
            "state": self.state,
            "attempt": self.attempt,
            "kills": self.kills,
            "steps": dict(self.steps),
            "step_by": dict(self.step_by),
            "outcome": self.outcome,
            "confirmation": self.confirmation,
            "placed_by": self.placed_by,
            "started_ms": self.started_ms,
            "killed_ms": self.killed_ms,
            "finished_ms": self.finished_ms,
        }


@dataclass
class Run:
    id: str
    created_ms: int = field(default_factory=now_ms)
    lock: threading.Lock = field(default_factory=threading.Lock)
    agents: dict = field(default_factory=lambda: {arm: [] for arm in ARMS})
    kills: int = 0
    #: Per step, when the lease of a killed holder lapses at the latest. Cleared once anyone
    #: completes the step.
    lease_lapses: Dict[str, int] = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return DATA_DIR / self.id

    def path(self, kind: str, arm: str) -> str:
        return str(self.dir / f"{kind}-{arm}.jsonl")

    @property
    def coordination_id(self) -> str:
        # Names the shared work for SCOPE_SHARED. Unique per run, so visitors never share a key.
        return f"order-{agents.ORDER_ID}-{self.id}"

    def session_id(self, number: int) -> str:
        # Stable across restarts: a restarted agent rejoins this session and recovers from it.
        return f"session-{self.id}-agent-{number}"

    def expired(self) -> bool:
        return now_ms() - self.created_ms > RUN_TTL_SECONDS * 1000

    def agent(self, arm: str, number: int) -> Agent:
        return self.agents[arm][number - 1]

    def snapshot(self) -> dict:
        with self.lock:
            arms = {
                arm: {
                    "orders": agents.read_records(self.path("orders", arm)),
                    "confirmations": agents.read_records(self.path("emails", arm)),
                    "agents": [a.snapshot() for a in self.agents[arm]],
                }
                for arm in ARMS
            }
            lapses = dict(self.lease_lapses)
        return {
            "id": self.id,
            "order_id": agents.ORDER_ID,
            "step_seconds": agents.STEP_SECONDS,
            "created_ms": self.created_ms,
            "now_ms": now_ms(),
            "expires_ms": self.created_ms + RUN_TTL_SECONDS * 1000,
            "max_agents": MAX_AGENTS_PER_RUN,
            "kills_left": MAX_KILLS_PER_RUN - self.kills,
            "lease_lapses_ms": lapses,
            "arms": arms,
        }


RUNS: dict[str, Run] = {}
RUNS_LOCK = threading.Lock()


def find_run(run_id: str) -> Run:
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "No such run. Runs expire after three minutes; start a new one.")
    return run


# --- Progress reports from agents -----------------------------------------------------------


def apply_event(run_id: str, arm: str, number: int, attempt: int, kind: str, fields: dict) -> None:
    """Records an agent's report, unless it comes from an attempt that has since been killed."""
    with RUNS_LOCK:
        run = RUNS.get(run_id)
    if run is None:
        return
    with run.lock:
        agent = run.agent(arm, number)
        if attempt != agent.attempt or agent.state != "running":
            return
        if kind == "step":
            step, state = fields["step"], fields["state"]
            agent.steps[step] = state
            if fields.get("by") is not None:
                agent.step_by[step] = fields["by"]
            if state in ("done", "received", "recovered"):
                run.lease_lapses.pop(step, None)
            if fields.get("confirmation"):
                agent.confirmation = fields["confirmation"]
                agent.placed_by = fields.get("placed_by")
        elif kind == "finished":
            agent.outcome = fields.get("outcome")
            agent.confirmation = fields.get("confirmation") or agent.confirmation
            agent.placed_by = fields.get("placed_by") or agent.placed_by


EVENTS = CTX.Queue()


def drain_events() -> None:
    while True:
        event = EVENTS.get()
        try:
            apply_event(event.pop("run"), event.pop("arm"), event.pop("number"),
                        event.pop("attempt"), event.pop("kind"), event)
        except Exception:  # an event for a run that is gone, or malformed: drop it
            log.exception("dropped an agent event")


threading.Thread(target=drain_events, name="events", daemon=True).start()


# --- Running, killing and restarting agents -------------------------------------------------


def run_attempt(run: Run, arm: str, number: int, attempt: int) -> str:
    """Runs one attempt of one agent. Returns finished, killed or error."""
    agent = run.agent(arm, number)
    if arm == CELLAFLOW:
        process = CTX.Process(
            target=agents.run_cellaflow_agent,
            args=(number, attempt, run.session_id(number), run.coordination_id,
                  run.path("orders", arm), run.path("emails", arm), ENGINE_TARGET, EVENTS, run.id),
            daemon=True,
        )
        process.start()
        agent.pid = process.pid
        process.join(AGENT_DEADLINE_SECONDS)
        if process.is_alive():
            process.kill()
            process.join()
            return "error"
        if agent.kill_requested:
            return "killed"
        return "finished" if process.exitcode == 0 else "error"

    stop = threading.Event()
    agent.stop = stop

    def report(kind: str, **fields: Any) -> None:
        apply_event(run.id, arm, number, attempt, kind, fields)

    try:
        agents.run_naive_agent(number, run.path("orders", arm), run.path("emails", arm), report, stop)
        return "finished"
    except agents.Stopped:
        return "killed"
    except Exception:
        log.exception("agent failed")
        return "error"


def supervise(run: Run, arm: str, number: int) -> None:
    """Runs an agent until it finishes, restarting it after every kill."""
    agent = run.agent(arm, number)
    while True:
        with run.lock:
            agent.attempt += 1
            agent.state = "running"
            agent.kill_requested = False
            attempt = agent.attempt
        result = run_attempt(run, arm, number, attempt)
        with run.lock:
            agent.pid = None
            if result == "killed":
                agent.state = "killed"
            else:
                agent.state = "done" if result == "finished" else "error"
                agent.finished_ms = now_ms()
        if result != "killed":
            return
        time.sleep(RESTART_DELAY_SECONDS)
        with run.lock:
            agent.state = "restarting"
            # A restart begins with nothing in memory. What the Cellaflow agent recovers
            # comes back from the engine, step by step, as it reports in.
            agent.steps = {s: "pending" for s in agents.STEPS}
            agent.step_by = {}


def launch(run: Run, count: int) -> None:
    """Starts `count` new agents in both arms at the same moment."""
    with run.lock:
        # Checked under the run's lock, so two concurrent requests cannot both pass it.
        if len(run.agents[NAIVE]) + count > MAX_AGENTS_PER_RUN:
            raise HTTPException(400, f"A run holds at most {MAX_AGENTS_PER_RUN} agents.")
        numbers = []
        for _ in range(count):
            number = len(run.agents[NAIVE]) + 1
            numbers.append(number)
            for arm in ARMS:
                run.agents[arm].append(Agent(number=number, arm=arm))
    for number in numbers:
        for arm in ARMS:
            threading.Thread(target=supervise, args=(run, arm, number),
                             name=f"{run.id}|{arm}|{number}", daemon=True).start()


def kill(run: Run, number: int) -> None:
    """Kills agent `number` on both sides at the same moment."""
    with run.lock:
        if not 1 <= number <= len(run.agents[NAIVE]):
            raise HTTPException(404, "No such agent.")
        if run.kills >= MAX_KILLS_PER_RUN:
            raise HTTPException(429, f"A run allows {MAX_KILLS_PER_RUN} kills. Start a new one.")
        targets = [run.agent(arm, number) for arm in ARMS]
        if not any(a.state == "running" for a in targets):
            raise HTTPException(409, "That agent is not running.")
        run.kills += 1
        killed_at = now_ms()
        for agent in targets:
            if agent.state != "running":
                continue
            agent.kill_requested = True
            agent.kills += 1
            agent.killed_ms = killed_at
            if agent.arm == CELLAFLOW:
                # A lease this agent held stops being renewed now, and lapses within its TTL.
                for step, state in agent.steps.items():
                    if state == "running" and step in ("order", "confirm"):
                        run.lease_lapses[step] = killed_at + LEASE_TTL_MS
                if agent.pid:
                    try:
                        os.kill(agent.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            elif agent.stop is not None:
                agent.stop.set()


# --- Limits and housekeeping -------------------------------------------------------------------

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
                    shutil.rmtree(RUNS[run_id].dir, ignore_errors=True)
                    del RUNS[run_id]
            for ip in [ip for ip, q in RECENT_RUNS_BY_IP.items() if not q or q[-1] < time.time() - 60]:
                del RECENT_RUNS_BY_IP[ip]


threading.Thread(target=reap_expired_runs, name="reaper", daemon=True).start()


# --- HTTP -----------------------------------------------------------------------------------

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
    run.dir.mkdir(parents=True, exist_ok=True)
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


@app.post("/api/runs/{run_id}/agents/{number}/kill")
def kill_agent(run_id: str, number: int) -> dict:
    run = find_run(run_id)
    if run.expired():
        raise HTTPException(410, "This run has ended. Start a new one.")
    kill(run, number)
    return run.snapshot()


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    return find_run(run_id).snapshot()
