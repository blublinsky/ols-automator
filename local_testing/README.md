# Local Testing

Run the full OLS Automator stack locally using SQLite and stub A2A agents.
Two agents with different skill domains test the RAG-based agent selection.

## Prerequisites

```bash
make install-deps-dev
```

## 1. Start the stub agents

Each agent loads its identity and skills from a YAML persona file.

Terminal 1 — cluster operations agent (alert analysis, remediation):

```bash
export OLS_AUTOMATOR_AUTH_TOKEN="local-dev-token"
STUB_AGENT_PERSONA=local_testing/agents/cluster-ops.yaml uv run uvicorn local_testing.stub_agent:app --port 9090
```

Terminal 2 — deployment agent (rollouts, scaling):

```bash
export OLS_AUTOMATOR_AUTH_TOKEN="local-dev-token"
STUB_AGENT_PERSONA=local_testing/agents/deployer.yaml uv run uvicorn local_testing.stub_agent:app --port 9091
```

Verify both agent cards:

```bash
curl -s http://localhost:9090/.well-known/agent-card.json | python -m json.tool
curl -s http://localhost:9091/.well-known/agent-card.json | python -m json.tool
```

## 2. Start the automator

Terminal 3:

```bash
export OLS_AUTOMATOR_AUTH_TOKEN="local-dev-token"
export OLS_AUTOMATOR_CONFIG=local_testing/config.yaml
export HF_HUB_OFFLINE=1  # use cached embedding model, skip network check
uv run uvicorn app.main:app --port 8080
```

The first run downloads the `all-MiniLM-L6-v2` embedding model (~80 MB)
from HuggingFace Hub.  Subsequent runs can use `HF_HUB_OFFLINE=1` to
skip the network check and load from cache.

At startup the automator discovers both agents, indexes their skills into
the RAG, and begins the reconciliation loop.

## 3. Test agent selection — alert (should route to cluster-ops)

```bash
curl -s -X POST http://localhost:8080/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "name": "HighMemoryUsage",
    "type": "alert",
    "content": "Pod frontend-abc is using 95% memory in namespace production",
    "ts": "2026-04-07T12:00:00Z"
  }' | python -m json.tool
```

## 4. Test agent selection — deployment (should route to deployer)

```bash
curl -s -X POST http://localhost:8080/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "name": "deploy-frontend",
    "type": "deployment",
    "content": "{\"image\": \"frontend:v2.1\", \"replicas\": 3}",
    "ts": "2026-04-07T14:30:00Z"
  }' | python -m json.tool
```

## 5. Watch the workflow

```bash
# List all work items
curl -s http://localhost:8080/api/v1/items | python -m json.tool

# Get details (replace KEY with actual key)
curl -s http://localhost:8080/api/v1/items/KEY | python -m json.tool
```

The deployment event flows straight through (automatic → completed).
The alert event stops at `approve` (manual phase) after assessment.

## 6. Approve the alert remediation

```bash
curl -s -X POST http://localhost:8080/api/v1/items/KEY/review \
  -H "Content-Type: application/json" \
  -d '{"command": "approve"}' | python -m json.tool
```

After approval, the remediation phase runs via cluster-ops, then completes.

## 7. Test denial and failed item cleanup

Send another alert so we have something to deny:

```bash
curl -s -X POST http://localhost:8080/api/v1/events \
  -H "Content-Type: application/json" \
  -d '{
    "name": "DiskPressure",
    "type": "alert",
    "content": "Node worker-3 is under disk pressure",
    "ts": "2026-04-08T10:00:00Z"
  }' | python -m json.tool
```

Wait a few seconds for the assess phase to complete, then deny it:

```bash
curl -s -X POST http://localhost:8080/api/v1/items/KEY/review \
  -H "Content-Type: application/json" \
  -d '{"command": "deny", "reason": "not safe to execute in production"}' | python -m json.tool
```

Verify the item is now in `failed` state with the denial reason:

```bash
curl -s http://localhost:8080/api/v1/items/KEY | python -m json.tool
```

Expected: `"phase": "failed"` and `"failure_reason": "not safe to execute in production"`

Delete the failed item:

```bash
curl -s -X DELETE http://localhost:8080/api/v1/items/KEY | python -m json.tool
```

Expected response: `{"status": "deleted", "key": "KEY"}`

Try deleting it again — should return 404:

```bash
curl -s -X DELETE http://localhost:8080/api/v1/items/KEY | python -m json.tool
```

Expected response: `{"detail": "No failed work item 'KEY' found"}`

## 8. Check metrics

```bash
curl -s http://localhost:8080/metrics | grep ols_automator
```

You should see counters for events received, phases completed/failed,
reviews (approve/deny), and the failed item gauge back to zero after deletion.

## Auth flow

Both the automator and the stub agents read `OLS_AUTOMATOR_AUTH_TOKEN`.
The automator sends it as a `Bearer` header; each stub agent validates it.
This mirrors the on-cluster service account token flow.

If the env var is unset, auth is skipped on both sides.

## Adding more test agents

Create a new persona file in `local_testing/agents/` following the same YAML
format, add the agent to `local_testing/config.yaml`, and run another instance
of the stub agent on a different port.

## Cleanup

```bash
rm -f local.db
```
