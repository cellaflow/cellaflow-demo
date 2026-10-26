# Demo design

The landing page lets a visitor start several agents that all try to place one order, add more
agents while the order is being placed, and see how many orders actually went through, with and
without Cellaflow.

## What the visitor does

1. Picks how many agents to start (1 to 8) and presses Start.
2. While the order is in flight, adds more agents.
3. Kills any running agent. It restarts two seconds later on its own.

Each agent is started twice at the same moment, once in each arm, so the two arms see identical
arrivals. A kill stops the agent in both arms at the same moment, and both restart together.

## What every agent does

Three steps: price the cart (1 s), place the order (5 s), send the customer a confirmation (2 s).

| | Without Cellaflow | With Cellaflow |
| :--- | :--- | :--- |
| Progress | Kept in memory | Recorded by the engine, step by step |
| Placing the order | Checks whether the order exists; if not, places it | `place_order`, a `SCOPE_SHARED` tool keyed on the order id: one agent places it, the rest get its confirmation |
| Sending the confirmation | Sends it | `send_confirmation`, shared the same way: one email |
| Killed and restarted | Starts again at step 1 with no record of what it did. If the order exists by then, it assumes the rest was handled and stops, so its confirmation is never sent | Gets back from the engine, by key, every result it already produced, and carries on from there. Pricing the cart has no side effect and simply runs again |
| Killed while holding a lease | Nothing is held | The lease stops being renewed and lapses within the engine's 20 s default. Until then the other agents wait; then one of them, or the restarted agent, does the step once |

The without-Cellaflow arm is the check-then-act guard agents typically write for themselves. It is
correct for one agent that never crashes, and every overlap or restart breaks it.

## What is real and what is simulated

- **Real:** the engine (`ghcr.io/cellaflow/cellaflow:0.2.0`, the open edition) and the Python SDK
  (`cellaflow==0.8.0` from PyPI). Every agent in the Cellaflow arm is its own OS process and its
  own session, and a kill is `SIGKILL` to that process. The engine sees a process die mid-call.
- **Stopped, not killed:** agents in the other arm hold nothing outside their own memory, so they
  are stopped at their next pause, which ends them the same way.
- **Simulated:** the shop and the outbox, which append to files after a pause. The agents run
  scripted steps, not an LLM: the demo is about what happens at the action.
- **States on the page:** each step reports whether it ran, waited on another agent's lease, was
  received from another agent, or was recovered from this agent's own record after a restart. A
  step is "waiting" only when the engine answered IN_PROGRESS for it.

The counters are read from each arm's shop and outbox files, never from what agents report.

## Limits

| Limit | Value | Setting |
| :--- | :--- | :--- |
| Agents per run | 8 | `DEMO_MAX_AGENTS_PER_RUN` |
| Kills per run | 12 | `DEMO_MAX_KILLS_PER_RUN` |
| Run lifetime | 180 s | `DEMO_RUN_TTL_SECONDS` |
| Active runs | 6 | `DEMO_MAX_ACTIVE_RUNS` |
| New runs per IP per minute | 6 | `DEMO_RUNS_PER_IP_PER_MINUTE` |
| Step durations | 1 s, 5 s, 2 s | `DEMO_PRICE_SECONDS`, `DEMO_ORDER_SECONDS`, `DEMO_CONFIRM_SECONDS` |
| Restart after a kill | 2 s | `DEMO_RESTART_DELAY_SECONDS` |

Runs live in memory and are dropped a minute after they expire, with their files. An agent process
still running after two minutes is stopped. The engine's data is wiped daily. Each run has its own
coordination id, and each agent its own session, so visitors never share keys.

## Security

Only Caddy is reachable from the internet (80 and 443). The demo API and the engine publish no
ports on the host; the engine is reachable only on the compose network.
