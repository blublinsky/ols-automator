"""Shared test fixtures — SQLite-backed AppConfig, DB session, HTTP client."""

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import app.models.config as config_module
from app.models.config import AppConfig
from app.models.models import (
    AUTOMATIC,
    COMPLETED,
    MANUAL,
    Base,
    PhaseConfig,
    Policy,
)


@pytest.fixture
async def app_config():
    """AppConfig wired to an in-memory SQLite database."""
    cfg = AppConfig(database_url="sqlite+aiosqlite://")
    cfg.policies = [
        Policy(
            name="test-policy",
            event_types=["alert"],
            phases=[
                PhaseConfig(
                    name="assess", mode=AUTOMATIC, operation="analyze this alert"
                ),
                PhaseConfig(name="approve", mode=MANUAL),
                PhaseConfig(
                    name="remediate",
                    mode=AUTOMATIC,
                    operation="execute remediation",
                ),
                PhaseConfig(name=COMPLETED),
            ],
        ),
    ]

    async with cfg.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    config_module._app_config = cfg
    yield cfg
    await cfg.engine.dispose()
    config_module._app_config = None


@pytest.fixture
async def session(app_config: AppConfig) -> AsyncSession:
    """Database session for direct DB operations in tests."""
    async with app_config.session_factory() as s:
        yield s


@pytest.fixture
async def client(app_config: AppConfig):
    """Async HTTP client connected to the FastAPI app (no lifespan)."""
    from app.main import app

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c
