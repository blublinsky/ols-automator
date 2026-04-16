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

        await _save_failed(
            "item-001", "something broke", failed_from_phase="assess"
        )

        async with app_config.session_factory() as s:
            item = await s.get(WorkItem, "item-001")
        assert item.phase == FAILED
        assert item.ready is False
        assert item.failure_reason == "something broke"
        assert item.failed_from_phase == "assess"

    async def test_missing_item_does_not_crash(self, app_config):
        await _save_failed("nonexistent", "reason", failed_from_phase=None)


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

    async def test_step_results_passed_to_next_agent(self, app_config, session):
        session.add(
            _make_item(
                phase="remediate",
                step_results={"assess": "root cause: OOM"},
            )
        )
        await session.commit()

        policy = app_config.policies[0]
        phase_config = policy.get_phase("remediate")
        item = _make_item(
            phase="remediate",
            step_results={"assess": "root cause: OOM"},
        )

        with patch(
            "app.services.orchestrator._invoke_agent",
            new_callable=AsyncMock,
            return_value="remediation done",
        ) as mock_invoke:
            await _run_phase(item, policy, phase_config)

        prompt = mock_invoke.call_args[0][0]
        passed_item = mock_invoke.call_args[0][1]
        assert "execute remediation" in prompt
        assert passed_item.step_results["assess"] == "root cause: OOM"

    async def test_step_results_included_in_prompt(self, app_config, session):
        from unittest.mock import MagicMock

        from app.models.models import AgentConfig
        from app.services.orchestrator import _invoke_agent

        item = _make_item(
            phase="remediate",
            step_results={"assess": "root cause: OOM"},
            event_content="Pod crashed",
        )

        mock_rag = MagicMock()
        mock_rag.match.return_value = ("test-agent", "test-skill")

        card = MagicMock()
        app_config.skill_rag = mock_rag
        app_config.agent_cards = {"test-agent": card}
        app_config.agents = [
            AgentConfig(
                name="test-agent",
                url="http://localhost:9999",
                invocation_timeout_seconds=90.0,
            ),
        ]

        with patch(
            "app.services.orchestrator.send_message",
            new_callable=AsyncMock,
            return_value="done",
        ) as mock_send:
            await _invoke_agent("execute remediation", item)

        assert mock_send.call_args.kwargs["timeout_seconds"] == 90.0
        prompt = mock_send.call_args[0][1]
        assert "Previous results:" in prompt
        assert "[assess]: root cause: OOM" in prompt
        assert "Pod crashed" in prompt

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
        assert wi.failed_from_phase == "assess"


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
