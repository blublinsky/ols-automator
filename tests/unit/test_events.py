"""Unit tests for the event ingestion endpoint."""

from datetime import datetime

import pytest


@pytest.mark.asyncio
class TestReceiveEvent:
    async def test_matching_event_stored(self, client):
        resp = await client.post(
            "/api/v1/events",
            json={
                "name": "cpu-high",
                "type": "alert",
                "content": "CPU at 99%",
                "ts": datetime.now().isoformat(),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["stored"] is True
        assert data["workload_id"]
        assert isinstance(data["workload_id"], str)

        got = await client.get(f"/api/v1/items/{data['workload_id']}")
        assert got.status_code == 200

    async def test_unmatched_type_skipped(self, client):
        resp = await client.post(
            "/api/v1/events",
            json={
                "name": "some-event",
                "type": "unknown",
                "content": "body",
                "ts": datetime.now().isoformat(),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "skipped"
        assert data["stored"] is None
        assert data["workload_id"] is None

    async def test_duplicate_deduplicated(self, client):
        event = {
            "name": "cpu-high",
            "type": "alert",
            "content": "CPU at 99%",
            "ts": "2025-01-01T00:00:00",
        }
        resp1 = await client.post("/api/v1/events", json=event)
        resp2 = await client.post("/api/v1/events", json=event)
        d1, d2 = resp1.json(), resp2.json()
        assert d1["stored"] is True
        assert d2["stored"] is False
        assert d1["workload_id"] == d2["workload_id"]

    async def test_invalid_payload_rejected(self, client):
        resp = await client.post("/api/v1/events", json={"name": "missing-fields"})
        assert resp.status_code == 422
