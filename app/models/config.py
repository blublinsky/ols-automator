"""Runtime configuration — AppConfig loaded from YAML."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.models import AgentConfig, Policy

if TYPE_CHECKING:
    from app.services.agent_rag import AgentSkillRAG

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://ols_automator:ols_automator@localhost:5432/ols_automator"
)
DATABASE_URL_ENV = "OLS_AUTOMATOR_DATABASE_URL"
CONFIG_PATH_ENV = "OLS_AUTOMATOR_CONFIG"


@dataclass
class AppConfig:
    """Central runtime configuration — everything the app needs to run."""

    database_url: str = DEFAULT_DATABASE_URL
    policies: list[Policy] = field(default_factory=list)
    agents: list[AgentConfig] = field(default_factory=list)

    engine: AsyncEngine = field(init=False, repr=False)
    session_factory: async_sessionmaker[AsyncSession] = field(init=False, repr=False)
    skill_rag: AgentSkillRAG | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        engine_kwargs: dict = {}
        if self.database_url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            engine_kwargs = {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            }
        self.engine = create_async_engine(
            self.database_url, echo=False, **engine_kwargs
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    def match_policy(self, event_type: str) -> Policy | None:
        """Find a policy that handles the given event type."""
        for policy in self.policies:
            if event_type in policy.event_types:
                return policy
        return None

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        """Load and validate configuration from a YAML file."""
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        schema = _ConfigFile(**raw)
        database_url = os.environ.get(DATABASE_URL_ENV, schema.database_url)

        return cls(
            database_url=database_url,
            policies=schema.policies,
            agents=schema.agents,
        )


class _ConfigFile(BaseModel):
    """Pydantic schema for the YAML config file."""

    database_url: str = DEFAULT_DATABASE_URL
    policies: list[Policy] = []
    agents: list[AgentConfig] = []


# --- Module-level access ---

_app_config: AppConfig | None = None


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load AppConfig from YAML (if given) or environment/defaults."""
    global _app_config
    if path is None:
        env_path = os.environ.get(CONFIG_PATH_ENV)
        if env_path and Path(env_path).is_file():
            path = env_path

    if path:
        _app_config = AppConfig.from_yaml(path)
        logger.info("Configuration loaded from %s", path)
    else:
        _app_config = AppConfig(
            database_url=os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL)
        )
        logger.info("Using default configuration")
    return _app_config


def get_config() -> AppConfig:
    """Return the current AppConfig; raises if not yet loaded."""
    if _app_config is None:
        raise RuntimeError("AppConfig not loaded — call load_config() first")
    return _app_config


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a database session for FastAPI dependency injection."""
    cfg = get_config()
    async with cfg.session_factory() as session:
        yield session
