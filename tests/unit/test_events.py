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

    async def test_duplicate_deduplicated(self, client):
        event = {
            "name": "cpu-high",
            "type": "alert",
            "content": "CPU at 99%",
            "ts": "2025-01-01T00:00:00",
        }
        resp1 = await client.post("/api/v1/events", json=event)
        resp2 = await client.post("/api/v1/events", json=event)
        assert resp1.json()["stored"] is True
        assert resp2.json()["stored"] is False

    async def test_invalid_payload_rejected(self, client):
        resp = await client.post("/api/v1/events", json={"name": "missing-fields"})
        assert resp.status_code == 422
