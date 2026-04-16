# OLS Automator

## Overall vision

![Extended OLS multi-agent automation overview](images/extended_OLS_multi_agent.png)

Automating how events are processed and executed does not necessarily mean abandoning the existing OLS server and starting from scratch. It can be considered an evolution of what we already have by adding a separate workflow component—this automator—alongside the OLS server, keeping the existing OLS server in place, and, when that fits the workflow, allowing the OLS server to participate as one of several available agents whose skills may be invoked for individual steps.

This architecture assumes an **ecosystem of agents** that the automator can leverage to execute actions in each workflow phase. The automator **orchestrates** remote agents (for example OLS) while still exposing its own HTTP API for events and work-item lifecycle. In other words, it is both a **workflow server** (events in, durable work items) and an **[Agent2Agent (A2A)](https://github.com/google/A2A) client** that invokes agents for individual steps. Standardizing outbound calls on the A2A protocol keeps the integration surface **agent-agnostic**: agents participate on equal terms as long as they implement the contract. A practical benefit is **reuse** of the same agents for fully automated runs and for interactive or human-guided execution elsewhere, without parallel integration stacks.

Because agents exist as services in their own right, they remain usable independently of the automator. When a workflow needs specialized or **just-in-time** capability, **adapter agents** can expose the same A2A interface and internally provision constrained execution (sandboxing, ephemeral tools, and similar) without pushing that complexity into the automator core. 
**Agent execution permissions** are not implemented in this repository yet; the envisioned approach follows Kubernetes-oriented patterns already familiar from OLS—**service account tokens** (or equivalent) on outbound agent calls, optionally combined with **impersonation** bound to predefined RBAC policies so step work runs under least-privilege identities. Two trust boundaries are worth keeping distinct: **who may submit or advance workflows** (API and policy boundaries) versus **which credential may invoke which agent or skill** (runtime invocation boundaries).

**Driving workflows from a UI or application** is a first-class use case: a client starts work by posting an event and receives a stable **`workload_id`** whenever a policy matches—including on **duplicate** submissions—so the same identifier can anchor dashboards and follow-up calls without guessing keys.

The extended OLS operator deploys and manages OLS Automator alongside the OLS server and the other components illustrated above.

OLS Automator is a policy-driven event processing engine receiving generic events via REST API, matching them to configurable multi-phase workflows, and delegating execution to remote configured agents discovered at startup.

## Architecture

```
                          +-----------------+
  Events (REST)  ------>  |  FastAPI app    |
                          |  POST /events   |
                          +--------+--------+
                                   |
                                   v
                          +--------+--------+
                          |    Database     |
                          |  (work items)   |
                          +--------+--------+
                                   ^
                                   |  polls
                          +--------+--------+
                          |  Reconciler     |
                          |  (background)   |
                          +--------+--------+
                                   |
                       +-----------+-----------+
                       |                       |
                 +-----+------+         +------+-----+
                 | RAG match  |         |  Manual    |
                 | operation  |         |  review    |
                 | to agent   |         |  (human)   |
                 +-----+------+         +------------+
                       |
                 +-----+------+
                 | A2A agent  |
                 | invocation |
                 +------------+
```

**Key design choices:**

- **Database as queue** -- work items are durable; crash-safe with lock/release
- **Policy-driven** -- workflows defined in YAML, not code
- **Agent-agnostic** -- agents discovered via A2A protocol, matched by RAG similarity
- **Reconciliation loop** -- continuously drives items through their workflow

## Workflow

Each policy defines an ordered list of phases. A work item moves through them:

```
event ─> [phase 1] ─> ... ─> [phase n] ─> [completed] ─> deleted
              │                  │
              │             (if manual)
              │               wait for
              │               review
              v                  │
           FAILED  <─────────────┘ (deny)
```

Phase types:
- **automatic + operation** -- RAG matches the operation to an A2A agent skill, invokes it
- **manual** -- item waits for human approval/denial via `POST /items/{key}/review`
- **completed** -- terminal phase, item is cleaned up by the reconciler

Implicit terminal states (not defined in policy):
- **failed** -- item lands here on manual denial or agent execution error; excluded from the reconciler loop and kept in the database for inspection via `GET /items/{key}`. On automatic failures, `failed_from_phase` on the item records the phase that was executing; for manual denial it is `null`.

Each phase's execution result is stored in the work item's `step_results` map, keyed by phase name. Subsequent phases can access results from earlier phases, enabling multi-step workflows where each agent builds on previous output. Results are visible via `GET /items/{key}`.

## Configuration

Configuration is loaded from a YAML file. Set the path via environment variable:

```bash
export OLS_AUTOMATOR_CONFIG=/path/to/config.yaml
```

### Config file format

```yaml
database_url: postgresql+asyncpg://user:pass@localhost:5432/ols_automator
embedding_model: all-MiniLM-L6-v2  # sentence-transformer model for RAG skill matching

policies:
  - name: alert-remediation
    event_types:
      - alert
    phases:
      - name: assess
        mode: automatic
        operation: "Analyze this alert and suggest remediation"
      - name: approve
        mode: manual
      - name: remediate
        mode: automatic
        operation: "Execute the approved remediation"
      - name: completed

agents:
  - name: ols
    url: http://lightspeed-app-server.openshift-lightspeed.svc:8080
    invocation_timeout_seconds: 120
  - name: cluster-agent
    url: http://cluster-agent.agents.svc:8080
    invocation_timeout_seconds: 600
```

**Timeouts (per agent):** `invocation_timeout_seconds` (optional, default `120`) caps each outbound A2A `send_message` to that agent while the reconciler runs an automatic phase. Agent card discovery at startup uses a fixed **30s** HTTP timeout (not configurable). Card fetch and `send_message` both use the same built-in **transient-error retries** with exponential backoff (not YAML-configurable).

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLS_AUTOMATOR_CONFIG` | -- | Path to YAML config file |
| `OLS_AUTOMATOR_DATABASE_URL` | `postgresql+asyncpg://...localhost...` | Overrides `database_url` from YAML |
| `OLS_AUTOMATOR_AUTH_TOKEN` | -- | Bearer token for agent calls (useful for local dev; on-cluster the projected SA token is used instead) |

## API

Full OpenAPI specification is available at runtime:

- **Swagger UI**: `http://<host>:8080/docs`
- **ReDoc**: `http://<host>:8080/redoc`
- **Raw JSON**: `http://<host>:8080/openapi.json`

A checked-in copy lives at [`docs/openapi.json`](docs/openapi.json). Regenerate it with `make schema`.

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/v1/events` | Ingest an event (JSON body). When a policy matches, the response includes **`workload_id`** (same value as the work item **`key`**) for both new inserts and duplicates—use **`GET /api/v1/items/{key}`** with that id to track progress |
| GET | `/api/v1/items` | List work items. Optional query parameters: `phase`, `event_type` (omit both to return all items). |
| GET | `/api/v1/items/{key}` | Work item detail (`key` is the workload id). Includes **`failed_from_phase`** when `phase` is `failed` after an automatic step error; `null` for manual denial |
| POST | `/api/v1/items/{key}/review` | JSON body: approve or deny a manual phase (`command`, optional `reason` on deny) |
| POST | `/api/v1/items/{key}/failed` | JSON body only: `{"command":"delete"}` removes a **failed** item; `{"command":"retry"}` re-queues the automatic phase recorded in **`failed_from_phase`** (returns **400** if there is no phase to restore, e.g. manual deny) |
| GET | `/readiness` | Readiness probe (DB check) |
| GET | `/liveness` | Liveness probe (reconciler check) |
| GET | `/metrics` | Prometheus metrics (see below) |

### API changes (summary)

- **Event ingestion** — `POST /api/v1/events` now returns **`workload_id`** whenever a policy matches (including **duplicate** events), so callers can correlate UI sessions to `GET /api/v1/items/{key}` without guessing keys.
- **Failed work items** — removal is **`POST /api/v1/items/{key}/failed`** with `{"command":"delete"}` (replaces **`DELETE /api/v1/items/{key}`**). **`{"command":"retry"}`** moves an item from **`failed`** back to the automatic phase named in **`failed_from_phase`** when that metadata exists.
- **Failure detail** — item detail exposes **`failed_from_phase`** for automatic failures to support retry semantics and operator visibility.

### Prometheus metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ols_automator_events_received_total` | Counter | `event_type`, `status` | Events received (status: stored, skipped, duplicate) |
| `ols_automator_reviews_total` | Counter | `command` | Manual review actions (approve, deny) |
| `ols_automator_failed_item_actions_total` | Counter | `command` | Failed-item actions (`delete`, `retry`) |
| `ols_automator_phases_completed_total` | Counter | `policy`, `phase` | Successful phase transitions |
| `ols_automator_phases_failed_total` | Counter | `policy`, `phase` | Phase failures |
| `ols_automator_items_waiting_manual` | Gauge | -- | Items awaiting manual approval |
| `ols_automator_items_in_flight` | Gauge | -- | Items being processed by agents |
| `ols_automator_items_ready` | Gauge | -- | Items ready for processing |
| `ols_automator_items_failed` | Gauge | -- | Items in failed state |
| `ols_automator_agent_invocation_duration_seconds` | Histogram | `agent` | A2A agent call duration |
| `ols_automator_reconcile_cycle_duration_seconds` | Histogram | -- | Reconciliation loop iteration duration |
| `ols_automator_items_released_stale_total` | Counter | -- | Items re-queued after stale lock timeout |

Request and response schemas for all endpoints are defined in the [OpenAPI spec](docs/openapi.json).
See also the [local testing guide](local_testing/README.md) for concrete `curl` examples.

## Extending the system

No code changes are needed to support new event types or agents — everything is config-driven.

### Adding a new event type

1. Add the new type to an existing policy's `event_types` list, or create a new policy with its own phases
2. Restart the automator

The reconciler will automatically pick up events matching the new type.

### Adding a new agent

1. Deploy an A2A-compatible agent (must serve `/.well-known/agent-card.json` and handle `send_message`)
2. Add it to the `agents` list in the config YAML
3. Restart the automator

At startup, the automator discovers the agent's skills and indexes them into the RAG. Operations are routed to the best-matching skill automatically — no explicit mapping required.

For local testing with stub agents, see the [local testing guide](local_testing/README.md#adding-more-test-agents).

## Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (`make install-tools` will install it if missing)

```bash
make install-deps-dev   # install runtime + dev dependencies
make run                # start with --reload on port 8080
make verify             # format + lint + test (all-in-one)
```

Tests use SQLite in-memory by default — no database setup required.

### Makefile targets

| Target | Description |
|--------|-------------|
| `install-tools` | Install `uv` if not already present |
| `install-deps` | Install runtime dependencies |
| `install-deps-dev` | Install runtime + dev dependencies |
| `update-deps` | Update lock file and sync |
| `run` | Run the service locally with auto-reload |
| `format` | Auto-format code with ruff |
| `lint` | Run ruff + mypy |
| `test` | Run all tests (alias for `test-unit`) |
| `test-unit` | Run unit tests |
| `verify` | Format, lint, and test in one go |
| `schema` | Generate OpenAPI schema into `docs/openapi.json` |
| `images` | Build container image with podman |
| `help` | Show all targets with descriptions |

