"""Generic event listener — receives events and stores them for processing."""

import hashlib
import logging
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import events_received_total
from app.models.config import get_config, get_session
from app.models.models import Event, Policy, WorkItem

_SANITIZE_RE = re.compile(r"[^a-z0-9-]")

logger = logging.getLogger(__name__)
router = APIRouter()


class EventResponse(BaseModel):
    """Response from the event ingestion endpoint."""

    status: str
    stored: bool | None = None
    reason: str | None = None


@router.post("/events", response_model=EventResponse)
async def receive_event(
    event: Event,
    session: AsyncSession = Depends(get_session),
):
    """Receive a single event, match against policy, and store for processing."""
    policy = get_config().match_policy(event.type)
    if not policy:
        events_received_total.labels(event_type=event.type, status="skipped").inc()
        return EventResponse(
            status="skipped", reason="no policy for this type of event"
        )

    first_phase = policy.first_phase().name
    stored = await _store_event(session, event, policy, first_phase)
    status = "stored" if stored else "duplicate"
    events_received_total.labels(event_type=event.type, status=status).inc()
    return EventResponse(status="ok", stored=stored)


async def _store_event(
    session: AsyncSession, event: Event, policy: Policy, phase: str
) -> bool:
    """Deduplicate and store an event to DB. The reconciler picks it up."""
    combined = f"{event.name}|{event.type}|{event.ts.isoformat()}"
    sanitized = _SANITIZE_RE.sub("-", event.name.lower())[:40]
    h = hashlib.sha256(combined.encode()).hexdigest()[:8]
    key = f"{sanitized}-{h}"

    item = WorkItem(
        key=key,
        event_name=event.name,
        event_type=event.type,
        event_content=event.content,
        phase=phase,
        ready=True,
        policy_name=policy.name,
    )
    # Rely on PK constraint for dedup instead of dialect-specific
    # INSERT ... ON CONFLICT to stay portable across DB backends.
    try:
        session.add(item)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.debug("Event %s already exists, skipping", key)
        return False

    logger.info("Stored event %s (policy: %s, phase: %s)", key, policy.name, phase)
    return True
