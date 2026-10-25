# Demo design

The landing page lets a visitor start several agents that all try to place one order, add more
agents while the order is being placed, and see how many orders actually went through, with and
without Cellaflow.

## What the visitor does

1. Picks how many agents to start (1 to 10) and presses Start.
2. While the order is in flight (6 seconds), adds more agents.
3. After it lands, adds more again.

Each agent is started twice at the same moment, once in each arm, so the two arms see identical
arrivals.

## The two arms

| | Without Cellaflow | With Cellaflow |
| :--- | :--- | :--- |
| What the agent does | Checks whether order #4417 exists; if not, places it | Calls `place_order` through the Python SDK, a `SCOPE_SHARED` tool keyed on the order id |
| Arrives while the order is in flight | Sees no order yet, places another | The engine has the call leased to another agent; waits on the lease, then gets that agent's result |
| Arrives after the order exists | Sees it and stands down | The engine returns the stored result; the tool does not run |
| Expected orders | One per agent that arrived in the window | One |

The without-Cellaflow arm is the check-then-act guard agents typically write for themselves. It is
not a straw man: it is correct for one agent, and every arrival during the 6 seconds breaks it.

## What is real and what is simulated

- **Real:** the engine (`ghcr.io/cellaflow/cellaflow:0.2.0`, the open edition), the Python SDK
  (`cellaflow==0.8.0` from PyPI), leases, fencing, the stored result. Agents in the Cellaflow arm
  are ordinary SDK clients, each in its own session, converging through `SCOPE_SHARED` and a
  coordination id unique to the run.
- **Simulated:** the shop. Placing an order appends to an in-memory ledger after a 6-second sleep.
  The agents are threads, not LLMs: the demo is about what happens at the action, and a model would
  only add cost and variance.
- **Waiting state:** shown when the SDK reports the engine answered IN_PROGRESS for that agent, read
  from the SDK's own log line on the agent's thread. Nothing on the page is inferred.

The counters are read from each arm's ledger, never from what agents report.

## Limits

| Limit | Value | Setting |
| :--- | :--- | :--- |
| Agents per run | 10 | `DEMO_MAX_AGENTS_PER_RUN` |
| Run lifetime | 120 s | `DEMO_RUN_TTL_SECONDS` |
| Active runs | 20 | `DEMO_MAX_ACTIVE_RUNS` |
| New runs per IP per minute | 6 | `DEMO_RUNS_PER_IP_PER_MINUTE` |
| Order duration | 6 s | `DEMO_ORDER_SECONDS` |

Runs live in memory and are dropped a minute after they expire. The engine's data is wiped daily.
Each run has its own coordination id, so visitors never share keys.

## Security

Only Caddy is reachable from the internet (80 and 443). The demo API and the engine publish no
ports on the host; the engine is reachable only on the compose network.
