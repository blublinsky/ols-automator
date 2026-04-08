"""Unit tests for the reconciliation orchestrator."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.models.models import AUTOMATIC, COMPLETED, FAILED, MANUAL, WorkItem
from app.services.orchestrator import (
    STALE_TIMEOUT,
    _cleanup_completed,
    _reconcile,
    _release_stale,
    _run_phase,
    _save_failed,
)


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
class TestSaveFailed:
    async def test_marks_item_failed(self, app_config, session):
        session.add(_make_item())
        await session.commit()

        await _save_failed("item-001", "something broke")

        async with app_config.session_factory() as s:
            item = await s.get(WorkItem, "item-001")
        assert item.phase == FAILED
        assert item.ready is False
        assert item.failure_reason == "something broke"

    async def test_missing_item_does_not_crash(self, app_config):
        await _save_failed("nonexistent", "reason")


@pytest.mark.asyncio
class TestCleanupCompleted:
    async def test_removes_completed_items(self, app_config, session):
        session.add(_make_item(key="done-1", phase=COMPLETED, ready=False))
        session.add(_make_item(key="active-1", phase="assess"))
        await session.commit()

        await _cleanup_completed()

        async with app_config.session_factory() as s:
            assert await s.get(WorkItem, "done-1") is None
            assert await s.get(WorkItem, "active-1") is not None

    async def test_noop_when_nothing_completed(self, app_config, session):
        session.add(_make_item(phase="assess"))
        await session.commit()
        await _cleanup_completed()


@pytest.mark.asyncio
class TestReleaseStale:
    async def test_releases_stale_automatic(self, app_config, session):
        stale = datetime.now(timezone.utc) - STALE_TIMEOUT - timedelta(minutes=5)
        session.add(
            _make_item(key="stale-1", ready=False, mode=AUTOMATIC, locked_at=stale)
        )
        await session.commit()

        await _release_stale()

        async with app_config.session_factory() as s:
            item = await s.get(WorkItem, "stale-1")
        assert item.ready is True
        assert item.locked_at is None

    async def test_skips_manual(self, app_config, session):
        stale = datetime.now(timezone.utc) - STALE_TIMEOUT - timedelta(minutes=5)
        session.add(
            _make_item(key="manual-1", ready=False, mode=MANUAL, locked_at=stale)
        )
        await session.commit()

        await _release_stale()

        async with app_config.session_factory() as s:
            item = await s.get(WorkItem, "manual-1")
        assert item.ready is False

    async def test_skips_recent(self, app_config, session):
        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.add(
            _make_item(key="recent-1", ready=False, mode=AUTOMATIC, locked_at=recent)
        )
        await session.commit()

        await _release_stale()

        async with app_config.session_factory() as s:
            item = await s.get(WorkItem, "recent-1")
        assert item.ready is False


@pytest.mark.asyncio
class TestRunPhase:
    async def test_manual_phase_waits(self, app_config, session):
        session.add(_make_item(phase="approve"))
        await session.commit()

        policy = app_config.policies[0]
        phase_config = policy.get_phase("approve")
        item = _make_item(phase="approve")

        await _run_phase(item, policy, phase_config)

        async with app_config.session_factory() as s:
            wi = await s.get(WorkItem, "item-001")
        assert wi.mode == MANUAL
        assert wi.ready is False

    async def test_automatic_invokes_agent(self, app_config, session):
        session.add(_make_item(phase="assess"))
        await session.commit()

        policy = app_config.policies[0]
        phase_config = policy.get_phase("assess")
        item = _make_item(phase="assess")

        with patch(
            "app.services.orchestrator._invoke_agent",
            new_callable=AsyncMock,
            return_value="assessment result",
        ):
            await _run_phase(item, policy, phase_config)

        async with app_config.session_factory() as s:
            wi = await s.get(WorkItem, "item-001")
        assert wi.phase == "approve"
        assert wi.ready is True
        assert wi.step_results.get("assess") == "assessment result"

    async def test_agent_failure_marks_failed(self, app_config, session):
        session.add(_make_item(phase="assess"))
        await session.commit()

        policy = app_config.policies[0]
        phase_config = policy.get_phase("assess")
        item = _make_item(phase="assess")

        with patch(
            "app.services.orchestrator._invoke_agent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("agent crashed"),
        ):
            await _run_phase(item, policy, phase_config)

        async with app_config.session_factory() as s:
            wi = await s.get(WorkItem, "item-001")
        assert wi.phase == FAILED
        assert "agent crashed" in wi.failure_reason


@pytest.mark.asyncio
class TestReconcile:
    async def test_picks_up_ready_items(self, app_config, session):
        session.add(_make_item(phase="assess"))
        await session.commit()

        with patch(
            "app.services.orchestrator._invoke_agent",
            new_callable=AsyncMock,
            return_value="done",
        ):
            await _reconcile()

        async with app_config.session_factory() as s:
            wi = await s.get(WorkItem, "item-001")
        assert wi.phase == "approve"

    async def test_ignores_unknown_event_type(self, app_config, session):
        session.add(_make_item(key="orphan", event_type="unknown"))
        await session.commit()

        await _reconcile()

        async with app_config.session_factory() as s:
            wi = await s.get(WorkItem, "orphan")
        assert wi.phase == "assess"
        assert wi.ready is True
