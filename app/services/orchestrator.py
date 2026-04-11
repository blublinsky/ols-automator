"""Reconciliation loop — polls DB for work and dispatches to handlers."""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta

from sqlalchemy import and_, case, delete, func, select, update

from app import metrics
from app.models.config import get_config
from app.models.models import PhaseConfig, Policy, WorkItem, MANUAL, COMPLETED, FAILED
from app.services.a2a_client import send_message

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
STALE_TIMEOUT = timedelta(hours=1)


# --- Reconciliation loop ---


async def run_loop():
    """Run cleanup, stale-release, and reconcile in a loop."""
    logger.info("Orchestrator loop started")
    while True:
        t0 = time.monotonic()
        try:
            await _cleanup_completed()
            await _release_stale()
            await _reconcile()
            await _update_gauges()
        except Exception:
            logger.exception("Reconcile cycle failed")
        metrics.reconcile_cycle_duration_seconds.observe(time.monotonic() - t0)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _reconcile():
    """Pick up ready work items and dispatch their handlers concurrently."""
    cfg = get_config()
    async with cfg.session_factory() as session:
        result = await session.execute(
            select(WorkItem).where(
                WorkItem.ready.is_(True),
                WorkItem.phase != COMPLETED,
                WorkItem.phase != FAILED,
            )
        )
        items = result.scalars().all()

    tasks = []
    for item in items:
        policy = cfg.match_policy(item.event_type)
        if not policy:
            continue
        phase_config = policy.get_phase(item.phase)
        if not phase_config:
            continue
        tasks.append(asyncio.create_task(_run_phase(item, policy, phase_config)))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _run_phase(item: WorkItem, policy: Policy, phase_config: PhaseConfig) -> None:
    """Claim an item, run its operation, compute next phase, persist results."""
    item_id = item.key

    cfg = get_config()
    async with cfg.session_factory() as session:
        cursor = await session.execute(
            update(WorkItem)
            .where(WorkItem.key == item_id, WorkItem.ready.is_(True))
            .values(
                ready=False,
                mode=phase_config.mode,
                locked_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        if cursor.rowcount == 0:  # type: ignore[attr-defined]
            return

    logger.info("Claimed %s: phase=%s, mode=%s", item_id, item.phase, phase_config.mode)

    if phase_config.mode == MANUAL:
        logger.info("%s awaiting manual review", item_id)
        return

    agent_result: str | None = None
    try:
        if phase_config.operation:
            agent_result = await _invoke_agent(phase_config.operation, item)
    except Exception as e:
        logger.exception(
            "Agent failed for (%s, %s) on %s", item.event_type, item.phase, item_id
        )
        await _save_failed(item_id, str(e))
        metrics.phases_failed_total.labels(policy=policy.name, phase=item.phase).inc()
        return

    next_config = policy.next_phase(item.phase)
    if not next_config:
        await _save_failed(
            item_id, f"No phase after '{item.phase}' in policy '{policy.name}'"
        )
        metrics.phases_failed_total.labels(policy=policy.name, phase=item.phase).inc()
        return

    async with cfg.session_factory() as session:
        wi = await session.get(WorkItem, item_id)
        if not wi:
            logger.warning("Work item %s disappeared, skipping", item_id)
            return
        if agent_result is not None:
            results = dict(wi.step_results or {})
            results[item.phase] = agent_result
            wi.step_results = results
        wi.phase = next_config.name
        wi.ready = True
        wi.locked_at = None
        await session.commit()

    metrics.phases_completed_total.labels(policy=policy.name, phase=item.phase).inc()
    logger.info(
        "(%s, %s) on %s → %s", item.event_type, item.phase, item_id, next_config.name
    )


async def _invoke_agent(operation: str, item: WorkItem) -> str:
    """Match an operation to an agent via RAG, then invoke it over A2A."""
    cfg = get_config()
    if not cfg.skill_rag:
        raise RuntimeError("No agents available for operation dispatch")

    match = cfg.skill_rag.match(operation)
    if not match:
        raise RuntimeError(f"No agent matched operation: {operation}")

    agent_name, skill_id = match
    logger.info(
        "RAG matched operation %r → agent=%s, skill=%s",
        operation[:80],
        agent_name,
        skill_id,
    )

    agent = next((a for a in cfg.agents if a.name == agent_name), None)
    if not agent:
        raise RuntimeError(f"Agent '{agent_name}' not in configuration")

    card = cfg.agent_cards.get(agent_name)
    if not card:
        raise RuntimeError(f"No cached card for agent '{agent_name}'")

    headers = agent.resolve_headers()

    parts = [operation, "", "Context:", item.event_content]
    if item.step_results:
        parts.append("")
        parts.append("Previous results:")
        for phase_name, result in item.step_results.items():
            parts.append(f"[{phase_name}]: {result}")
    prompt = "\n".join(parts)
    t0 = time.monotonic()
    try:
        return await send_message(card, prompt, headers, skill_id)
    finally:
        metrics.agent_invocation_duration_seconds.labels(agent=agent_name).observe(
            time.monotonic() - t0
        )


async def _save_failed(item_id: str, reason: str = ""):
    """Mark a work item as failed with an optional reason."""
    async with get_config().session_factory() as session:
        wi = await session.get(WorkItem, item_id)
        if not wi:
            logger.warning("Work item %s disappeared, skipping", item_id)
            return
        wi.phase = FAILED
        wi.ready = False
        wi.locked_at = None
        wi.failure_reason = reason
        await session.commit()


async def _cleanup_completed():
    """Delete work items that have reached the completed phase."""
    async with get_config().session_factory() as session:
        result = await session.execute(
            delete(WorkItem).where(WorkItem.phase == COMPLETED).returning(WorkItem.key)
        )
        removed = result.scalars().all()
        await session.commit()

    if removed:
        logger.info("Cleaned up %d completed items", len(removed))


async def _release_stale():
    """Re-queue automatic work items that have been locked too long."""
    now = datetime.now(timezone.utc)
    async with get_config().session_factory() as session:
        result = await session.execute(
            select(WorkItem).where(
                WorkItem.ready.is_(False),
                WorkItem.locked_at.is_not(None),
            )
        )
        items = result.scalars().all()

        released = []
        for item in items:
            if item.mode == MANUAL:
                continue
            locked: datetime = item.locked_at  # type: ignore[assignment]
            if locked.tzinfo is None:
                locked = locked.replace(tzinfo=timezone.utc)
            if now - locked > STALE_TIMEOUT:
                item.ready = True
                item.locked_at = None
                released.append(item.key)

        if released:
            await session.commit()
            metrics.items_released_stale_total.inc(len(released))
            logger.warning("Released %d stale items: %s", len(released), released)


async def _update_gauges():
    """Refresh Prometheus gauges from current DB state."""
    async with get_config().session_factory() as session:
        row = (
            await session.execute(
                select(
                    func.count(
                        case(
                            (
                                and_(
                                    WorkItem.mode == MANUAL, WorkItem.ready.is_(False)
                                ),
                                1,
                            )
                        )
                    ).label("manual"),
                    func.count(
                        case(
                            (
                                and_(
                                    WorkItem.locked_at.is_not(None),
                                    WorkItem.mode != MANUAL,
                                ),
                                1,
                            )
                        )
                    ).label("in_flight"),
                    func.count(
                        case(
                            (
                                and_(
                                    WorkItem.ready.is_(True),
                                    WorkItem.phase != COMPLETED,
                                ),
                                1,
                            )
                        )
                    ).label("ready"),
                    func.count(case((WorkItem.phase == FAILED, 1))).label("failed"),
                ).select_from(WorkItem)
            )
        ).one()
    metrics.items_waiting_manual.set(row.manual)
    metrics.items_in_flight.set(row.in_flight)
    metrics.items_ready.set(row.ready)
    metrics.items_failed.set(row.failed)
