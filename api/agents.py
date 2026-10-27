"""The agents the demo runs, and the shop they order from.

Every agent does the same job in three steps: price the cart, place the order, send the
customer a confirmation. The agents differ in one way only:

- **Without Cellaflow**, an agent keeps its progress in memory. It checks whether the order
  exists before placing it, which is the guard agents usually write for themselves. When it is
  killed and restarted, it starts again from step 1, with no record of what it already did.
- **With Cellaflow**, an agent is an ordinary client of the Python SDK, running in its own OS
  process. Placing the order and sending the confirmation are tools shared across every agent in
  the run, so the engine leases each one to a single agent and hands the others its result. When
  the process is killed with SIGKILL and restarted, the engine hands it back every result it had
  already produced, by the same keys, without running it again.

The shop and the outbox are append-only files guarded by ``flock``, so processes can share them.
The counters on the page are read from these files, never from what an agent reports.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

ORDER_ID = "4417"
TOTAL_CENTS = 2499

#: How long each step takes. Placing the order is the long one, so there is time to add or kill
#: agents while it is in flight.
STEP_SECONDS = {
    "price": float(os.environ.get("DEMO_PRICE_SECONDS", "1")),
    "order": float(os.environ.get("DEMO_ORDER_SECONDS", "5")),
    "confirm": float(os.environ.get("DEMO_CONFIRM_SECONDS", "2")),
}
STEPS = ("price", "order", "confirm")


# --- The shop and the outbox ------------------------------------------------------------------


def append_record(path: str, record: Dict[str, Any]) -> int:
    """Appends one line and returns how many lines the file now has."""
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            count = sum(1 for _ in fh) + 1
            fh.write(json.dumps(record) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            return count
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def read_records(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def place_order_at_shop(orders_path: str, agent_number: int) -> Dict[str, Any]:
    """The shop. Takes no idempotency key: every call places one more order."""
    n = append_record(orders_path, {"agent": agent_number, "at_ms": int(time.time() * 1000)})
    return {"confirmation": f"ORD-{ORDER_ID}-{n}", "placed_by": agent_number}


def send_email(emails_path: str, agent_number: int, confirmation: str) -> Dict[str, Any]:
    """The outbox. Every call sends one more email to the customer."""
    append_record(
        emails_path,
        {"agent": agent_number, "confirmation": confirmation, "at_ms": int(time.time() * 1000)},
    )
    return {"emailed": confirmation, "sent_by": agent_number}


# --- Without Cellaflow ------------------------------------------------------------------------


class Stopped(Exception):
    """Raised inside a stopped agent at its next pause, the way a killed process just ends."""


def run_naive_agent(
    number: int,
    orders_path: str,
    emails_path: str,
    report: Callable[..., None],
    stopped: threading.Event,
) -> None:
    """Three steps with nothing durable. A restart begins again at step 1."""

    def pause(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if stopped.is_set():
                raise Stopped()
            time.sleep(0.05)

    report("step", step="price", state="running")
    pause(STEP_SECONDS["price"])
    report("step", step="price", state="done")

    if read_records(orders_path):
        # Someone placed it already, so this agent assumes the rest was handled too.
        report("step", step="order", state="skipped")
        report("step", step="confirm", state="skipped")
        report("finished", outcome="saw_order")
        return

    report("step", step="order", state="running")
    pause(STEP_SECONDS["order"])
    placed = place_order_at_shop(orders_path, number)
    report("step", step="order", state="done", confirmation=placed["confirmation"], placed_by=number)

    report("step", step="confirm", state="running")
    pause(STEP_SECONDS["confirm"])
    send_email(emails_path, number, placed["confirmation"])
    report("step", step="confirm", state="done")
    report("finished", outcome="placed", confirmation=placed["confirmation"], placed_by=number)


# --- With Cellaflow ---------------------------------------------------------------------------


def run_cellaflow_agent(
    number: int,
    attempt: int,
    session_id: str,
    coordination_id: str,
    orders_path: str,
    emails_path: str,
    engine_target: str,
    events: Any,
    run_id: str,
) -> None:
    """Runs in its own process. Killing this process is a real crash, as the engine sees it."""
    from cellaflow import IdempotencyScope, step, tool, workflow

    def report(kind: str, **fields: Any) -> None:
        events.put({"run": run_id, "arm": "with_cellaflow", "number": number, "attempt": attempt,
                    "kind": kind, **fields})

    current: Dict[str, Optional[str]] = {"step": None}
    ran: Dict[str, bool] = {}

    class WaitingDetector(logging.Handler):
        """The SDK logs this line each time the engine answers IN_PROGRESS for a call."""

        def emit(self, record: logging.LogRecord) -> None:
            if "in progress by another worker" in record.getMessage() and current["step"]:
                report("step", step=current["step"], state="waiting")

    sdk_log = logging.getLogger("cellaflow.decorators")
    sdk_log.setLevel(logging.INFO)
    sdk_log.addHandler(WaitingDetector())

    agent_id = f"agent-{number}"

    # Pricing belongs to this agent in this run, so that is what its key names. A restarted agent
    # runs in a new session and gets its own price back from the engine by the same key.
    @step(agent_id=agent_id, idempotency_key=f"{coordination_id}:price:{agent_id}")
    def price_cart(order_id: str) -> int:
        ran["price"] = True
        report("step", step="price", state="running")
        time.sleep(STEP_SECONDS["price"])
        return TOTAL_CENTS

    # Shared by every agent in the run: the engine leases it to one of them, and the rest get
    # its result. The order id identifies the work; the total does not need to match.
    @tool(agent_id=agent_id, tool_name="place_order", scope=IdempotencyScope.SCOPE_SHARED,
          shared_on=["order_id"])
    def place_order(order_id: str, total_cents: int) -> dict:
        ran["order"] = True
        report("step", step="order", state="running")
        time.sleep(STEP_SECONDS["order"])
        return place_order_at_shop(orders_path, number)

    @tool(agent_id=agent_id, tool_name="send_confirmation", scope=IdempotencyScope.SCOPE_SHARED,
          shared_on=["order_id"])
    def send_confirmation(order_id: str, confirmation: str) -> dict:
        ran["confirm"] = True
        report("step", step="confirm", state="running")
        time.sleep(STEP_SECONDS["confirm"])
        return send_email(emails_path, number, confirmation)

    def finish(name: str, by: Optional[int] = None, **fields: Any) -> None:
        if ran.get(name):
            state = "done"
        elif by is not None and by != number:
            # The body did not run: another agent did this, and the engine handed over its result.
            state = "received"
        else:
            # The body did not run in this attempt: the engine returned this agent's own earlier
            # work, recorded before it was killed.
            state = "recovered"
        report("step", step=name, state=state, by=by, **fields)

    @workflow(version="1.0.0", target=engine_target)
    def order_agent(order_id: str) -> dict:
        current["step"] = "price"
        total = price_cart(order_id)
        finish("price")

        current["step"] = "order"
        order = place_order(order_id, total)
        finish("order", by=order["placed_by"], confirmation=order["confirmation"],
               placed_by=order["placed_by"])

        current["step"] = "confirm"
        sent = send_confirmation(order_id, order["confirmation"])
        finish("confirm", by=sent["sent_by"])
        return order

    report("started")
    order = order_agent(ORDER_ID, _session_id=session_id, _coordination_id=coordination_id)
    # Whoever placed it, in this attempt or before a restart.
    outcome = "placed" if order["placed_by"] == number else "received"
    report("finished", outcome=outcome, confirmation=order["confirmation"],
           placed_by=order["placed_by"])
