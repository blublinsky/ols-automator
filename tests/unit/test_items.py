"""Unit tests for work-item endpoints — list, detail, review."""

import pytest

from app.models.models import MANUAL, WorkItem


def _make_item(**overrides) -> WorkItem:
    defaults = dict(
        key="item-001",
        event_name="cpu-high",
        event_type="alert",
        event_content="CPU 99%",
        phase="assess",
        ready=True,
        policy_name="test-policy",
    )
    defaults.update(overrides)
    return WorkItem(**defaults)


@pytest.mark.asyncio
class TestListItems:
    async def test_empty_list(self, client):
        resp = await client.get("/api/v1/items")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_with_items(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item())
            await s.commit()

        resp = await client.get("/api/v1/items")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "item-001"

    async def test_filter_by_phase(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="a", phase="assess"))
            s.add(_make_item(key="b", phase="approve", ready=False, mode=MANUAL))
            await s.commit()

        resp = await client.get("/api/v1/items", params={"phase": "approve"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["key"] == "b"

    async def test_filter_by_event_type(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item())
            await s.commit()

        resp = await client.get("/api/v1/items", params={"event_type": "unknown"})
        assert resp.json() == []


@pytest.mark.asyncio
class TestGetItem:
    async def test_found(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="test-002"))
            await s.commit()

        resp = await client.get("/api/v1/items/test-002")
        assert resp.status_code == 200
        assert resp.json()["key"] == "test-002"

    async def test_not_found(self, client):
        resp = await client.get("/api/v1/items/nonexistent")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestReviewItem:
    async def test_approve_advances_phase(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="rev-001", phase="approve", ready=False, mode=MANUAL))
            await s.commit()

        resp = await client.post(
            "/api/v1/items/rev-001/review", json={"command": "approve"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "approve"
        assert data["phase"] == "remediate"

    async def test_deny_marks_failed(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="rev-002", phase="approve", ready=False, mode=MANUAL))
            await s.commit()

        resp = await client.post(
            "/api/v1/items/rev-002/review",
            json={"command": "deny", "reason": "too risky"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deny"
        assert data["phase"] == "failed"

    async def test_not_manual_rejected(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="rev-003", phase="assess"))
            await s.commit()

        resp = await client.post(
            "/api/v1/items/rev-003/review", json={"command": "approve"}
        )
        assert resp.status_code == 400

    async def test_item_not_found(self, client):
        resp = await client.post(
            "/api/v1/items/nonexistent/review", json={"command": "approve"}
        )
        assert resp.status_code == 404

    async def test_invalid_command_rejected(self, client, app_config):
        async with app_config.session_factory() as s:
            s.add(_make_item(key="rev-004", phase="approve", ready=False, mode=MANUAL))
            await s.commit()

        resp = await client.post(
            "/api/v1/items/rev-004/review", json={"command": "invalid"}
        )
        assert resp.status_code == 422
