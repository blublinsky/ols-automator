# OLS Automator

Policy-driven event processing engine. Receives generic events via REST API, matches them to configurable multi-phase workflows, and delegates execution to remote A2A agents discovered at startup.

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
    timeout: 30
  - name: cluster-agent
    url: http://cluster-agent.agents.svc:8080
```

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
| POST | `/api/v1/events` | Ingest an event |
| GET | `/api/v1/items` | List work items (filterable by `phase`, `event_type`) |
| GET | `/api/v1/items/{key}` | Work item detail |
| POST | `/api/v1/items/{key}/review` | Approve or deny a manual phase |
| GET | `/readiness` | Readiness probe (DB check) |
| GET | `/liveness` | Liveness probe (reconciler check) |
| GET | `/metrics` | Prometheus metrics (see below) |

### Prometheus metrics

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `ols_automator_events_received_total` | Counter | `event_type`, `status` | Events received (status: stored, skipped, duplicate) |
| `ols_automator_reviews_total` | Counter | `command` | Manual review actions (approve, deny) |
| `ols_automator_phases_completed_total` | Counter | `policy`, `phase` | Successful phase transitions |
| `ols_automator_phases_failed_total` | Counter | `policy`, `phase` | Phase failures |
| `ols_automator_items_waiting_manual` | Gauge | -- | Items awaiting manual approval |
| `ols_automator_items_in_flight` | Gauge | -- | Items being processed by agents |
| `ols_automator_items_ready` | Gauge | -- | Items ready for processing |
| `ols_automator_items_failed` | Gauge | -- | Items in failed state |
| `ols_automator_agent_invocation_duration_seconds` | Histogram | `agent` | A2A agent call duration |
| `ols_automator_reconcile_cycle_duration_seconds` | Histogram | -- | Reconciliation loop iteration duration |
| `ols_automator_items_released_stale_total` | Counter | -- | Items re-queued after stale lock timeout |

### Event payload

The `content` field is an opaque string passed through to the agent as context. It can be plain text or a JSON string -- the automator does not parse it.

```json
{
  "name": "HighMemoryUsage",
  "type": "alert",
  "content": "Pod frontend-abc is using 95% memory in namespace production",
  "ts": "2026-04-07T12:00:00Z"
}
```

```json
{
  "name": "deploy-frontend",
  "type": "deployment",
  "content": "{\"image\": \"frontend:v2.1\", \"replicas\": 3, \"namespace\": \"production\"}",
  "ts": "2026-04-07T14:30:00Z"
}
```

### Review payload

```json
{"command": "approve"}
{"command": "deny", "reason": "not safe to execute in production"}
```

## Development

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

## Project layout

```
app/
  main.py                  # FastAPI app, lifespan, probes
  models/
    models.py              # Pydantic models (Event, Policy, AgentConfig) + SQLAlchemy ORM (WorkItem)
    config.py              # AppConfig loaded from YAML, DB engine, session management
  routes/
    events.py              # POST /events -- ingest and deduplicate
    items.py               # GET/POST /items -- list, detail, review
  services/
    orchestrator.py        # Reconciliation loop, phase execution, A2A invocation
    a2a_client.py          # A2A protocol client (fetch card, send message)
    agent_rag.py           # Hybrid RAG for agent skill discovery and matching
```
