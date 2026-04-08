"""OLS Automator — policy-driven event processing engine."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app
from sqlalchemy import text

from app.models.config import get_config, load_config
from app.models.models import Base
from app.routes import events, items
from app.services import orchestrator
from app.services.agent_rag import discover_agents

logger = logging.getLogger(__name__)

_loop_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(_application: FastAPI):
    """Load config, create tables, discover agents, start reconciliation loop."""
    global _loop_task
    cfg = load_config()

    async with cfg.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    cfg.skill_rag = await discover_agents(cfg.agents)

    _loop_task = asyncio.create_task(orchestrator.run_loop())
    yield
    _loop_task.cancel()
    try:
        await _loop_task
    except asyncio.CancelledError:
        pass
    _loop_task = None
    await cfg.engine.dispose()


app = FastAPI(
    title="OLS Automator",
    description="Policy-driven event processing engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(events.router, prefix="/api/v1", tags=["events"])
app.include_router(items.router, prefix="/api/v1", tags=["items"])

metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)


@app.get("/readiness")
async def readiness():
    """Readiness probe — verifies the database is reachable."""
    try:
        cfg = get_config()
        async with cfg.session_factory() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.get("/liveness")
async def liveness():
    """Liveness probe — checks the reconciler is still running."""
    if _loop_task and _loop_task.done():
        return JSONResponse(
            {"status": "unhealthy", "reason": "reconciler stopped"},
            status_code=503,
        )
    return {"status": "ok"}
