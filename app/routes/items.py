"""Work item endpoints — list, detail, review, and delete."""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.metrics import reviews_total
from app.models.config import get_config, get_session
from app.models.models import WorkItem, MANUAL, FAILED

logger = logging.getLogger(__name__)
router = APIRouter()


# --- Response / request models ---


class WorkItemSummary(BaseModel):
    """Compact view returned by list endpoint."""

    key: str
    event_name: str
    event_type: str
    phase: str
    manual: bool
    policy_name: str
    created_at: datetime


class WorkItemDetail(WorkItemSummary):
    """Full view returned by detail endpoint."""

    event_content: str
    ready: bool
    step_results: dict[str, str]
    failure_reason: str | None
    updated_at: datetime


class ReviewRequest(BaseModel):
    command: Literal["approve", "deny"]
    reason: str = ""


class ReviewResponse(BaseModel):
    status: str
    key: str
    phase: str


# --- Endpoints ---


@router.get("/items", response_model=list[WorkItemSummary], summary="List work items")
async def list_items(
    phase: str | None = Query(None, description="Filter by phase"),
    event_type: str | None = Query(None, description="Filter by event type"),
    session: AsyncSession = Depends(get_session),
):
    """List work items, optionally filtered by phase or event type."""
    stmt = select(WorkItem).order_by(WorkItem.created_at.desc())
    if phase:
        stmt = stmt.where(WorkItem.phase == phase)
    if event_type:
        stmt = stmt.where(WorkItem.event_type == event_type)
    result = await session.execute(stmt)
    return [_to_summary(item) for item in result.scalars().all()]


@router.get(
    "/items/{key}", response_model=WorkItemDetail, summary="Get work item detail"
)
async def get_item(
    key: str,
    session: AsyncSession = Depends(get_session),
):
    """Get full details of a work item."""
    item = await session.get(WorkItem, key)
    if not item:
        raise HTTPException(404, f"Work item {key} not found")
    return _to_detail(item)


@router.post(
    "/items/{key}/review",
    response_model=ReviewResponse,
    summary="Approve or deny a work item",
)
async def review_item(
    key: str,
    body: ReviewRequest,
    session: AsyncSession = Depends(get_session),
):
    """Approve or deny a work item in a manual phase."""
    item = await session.get(WorkItem, key)
    if not item:
        raise HTTPException(404, f"Work item {key} not found")

    if item.mode != MANUAL:
        raise HTTPException(
            400, f"Work item is not in a manual phase (mode={item.mode})"
        )

    event_type: str = item.event_type  # type: ignore[assignment]
    current_phase: str = item.phase  # type: ignore[assignment]

    policy = get_config().match_policy(event_type)
    if not policy:
        raise HTTPException(500, f"No policy for event type '{event_type}'")

    next_config = policy.next_phase(current_phase)
    if not next_config:
        raise HTTPException(400, f"No next phase after '{current_phase}'")

    match body.command:
        case "approve":
            item.phase = next_config.name
            item.ready = True
            logger.info("Work item %s approved, advancing to %s", key, next_config.name)

        case "deny":
            item.phase = FAILED
            item.ready = False
            item.failure_reason = body.reason
            logger.info("Work item %s denied: %s", key, body.reason)

    item.mode = None
    item.locked_at = None
    await session.commit()

    reviews_total.labels(command=body.command).inc()
    phase: str = item.phase  # type: ignore[assignment]
    return ReviewResponse(status=body.command, key=key, phase=phase)


class DeleteResponse(BaseModel):
    status: str
    key: str


@router.delete(
    "/items/{key}",
    response_model=DeleteResponse,
    summary="Delete a failed work item",
)
async def delete_item(
    key: str,
    session: AsyncSession = Depends(get_session),
):
    """Delete a work item that is in the failed state."""
    result = await session.execute(
        select(WorkItem).where(WorkItem.key == key, WorkItem.phase == FAILED)
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(404, f"No failed work item '{key}' found")

    await session.delete(item)
    await session.commit()
    logger.info("Deleted failed work item %s", key)
    return DeleteResponse(status="deleted", key=key)


# --- ORM to Pydantic conversion ---


def _to_summary(item: WorkItem) -> WorkItemSummary:
    return WorkItemSummary(
        key=item.key,
        event_name=item.event_name,
        event_type=item.event_type,
        phase=item.phase,
        manual=item.mode == MANUAL,
        policy_name=item.policy_name,
        created_at=item.created_at,
    )


def _to_detail(item: WorkItem) -> WorkItemDetail:
    return WorkItemDetail(
        key=item.key,
        event_name=item.event_name,
        event_type=item.event_type,
        event_content=item.event_content,
        phase=item.phase,
        manual=item.mode == MANUAL,
        ready=item.ready,
        policy_name=item.policy_name,
        step_results=item.step_results,
        failure_reason=item.failure_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
