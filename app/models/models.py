"""Data models — pure definitions, no mutable state."""

import os
from datetime import datetime
from typing import Final, Literal, Self

from pydantic import BaseModel, model_validator
from sqlalchemy import String, Text, Boolean, DateTime, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql.functions import now

MANUAL: Final = "manual"
AUTOMATIC: Final = "automatic"
COMPLETED: Final = "completed"
FAILED: Final = "failed"

SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
AUTH_TOKEN_ENV = "OLS_AUTOMATOR_AUTH_TOKEN"


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all ORM models."""


# --- Event ---


class Event(BaseModel):
    """Inbound event payload."""

    name: str
    type: str
    content: str
    ts: datetime


# --- Policy ---


class PhaseConfig(BaseModel):
    """Single phase within a policy workflow."""

    name: str
    mode: Literal["automatic", "manual"] = AUTOMATIC
    operation: str | None = None


class Policy(BaseModel):
    """Workflow definition: event type → ordered list of phases."""

    name: str
    event_types: list[str]
    phases: list[PhaseConfig]

    @model_validator(mode="after")
    def _validate_phases(self) -> Self:
        if len(self.phases) < 2:
            raise ValueError(f"Policy '{self.name}' must define at least two phases")
        if self.phases[-1].name != COMPLETED:
            raise ValueError(f"Policy '{self.name}' must end with 'completed' phase")
        return self

    def first_phase(self) -> PhaseConfig:
        """Return the first phase in the workflow."""
        return self.phases[0]

    def next_phase(self, current_phase: str) -> PhaseConfig | None:
        """Return the phase after current_phase, or None if at the end."""
        for i, phase in enumerate(self.phases):
            if phase.name == current_phase and i + 1 < len(self.phases):
                return self.phases[i + 1]
        return None

    def get_phase(self, name: str) -> PhaseConfig | None:
        """Look up a phase by name."""
        for phase in self.phases:
            if phase.name == name:
                return phase
        return None


# --- Agent ---


class AgentConfig(BaseModel):
    """A remote agent reachable via A2A."""

    name: str
    url: str
    headers: dict[str, str] | None = None
    token_path: str = SA_TOKEN_PATH
    timeout: int = 30

    def resolve_headers(self) -> dict[str, str]:
        """Build request headers, resolving a Bearer token from (in order):

        1. Explicit ``Authorization`` in ``self.headers``
        2. ``OLS_AUTOMATOR_AUTH_TOKEN`` environment variable
        3. Projected service-account token file on disk
        """
        resolved = dict(self.headers or {})
        if "Authorization" not in resolved:
            token = os.environ.get(AUTH_TOKEN_ENV, "").strip()
            if not token and os.path.isfile(self.token_path):
                with open(self.token_path, encoding="utf-8") as f:
                    token = f.read().strip()
            if token:
                resolved["Authorization"] = f"Bearer {token}"
        return resolved


# --- WorkItem ---


class WorkItem(Base):
    """Database model for a unit of work being processed."""

    __tablename__ = "items"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)

    event_name: Mapped[str] = mapped_column(String(256))
    event_type: Mapped[str] = mapped_column(String(256), index=True)
    event_content: Mapped[str] = mapped_column(Text)

    phase: Mapped[str] = mapped_column(String(64))
    ready: Mapped[bool] = mapped_column(Boolean, default=False)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    policy_name: Mapped[str] = mapped_column(String(256))
    step_results: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=now(), onupdate=now()
    )
