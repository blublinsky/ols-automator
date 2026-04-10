"""Stub A2A agent for local testing.

A data-driven agent that loads its identity, skills, and canned responses
from a YAML persona file.  Multiple instances with different personas can
run side-by-side to test RAG-based agent selection.

    STUB_AGENT_PERSONA=local_testing/agents/cluster-ops.yaml uv run uvicorn local_testing.stub_agent:app --port 9090
    STUB_AGENT_PERSONA=local_testing/agents/deployer.yaml   uv run uvicorn local_testing.stub_agent:app --port 9091
"""

import logging
import os
import sys

import yaml
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AFastAPIApplication
from a2a.server.events import EventQueue, InMemoryQueueManager
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentProvider,
    AgentSkill,
    Part,
    TextPart,
)
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load persona
# ---------------------------------------------------------------------------

PERSONA_PATH = os.environ.get(
    "STUB_AGENT_PERSONA", "local_testing/agents/cluster-ops.yaml"
)
AUTH_TOKEN = os.environ.get("OLS_AUTOMATOR_AUTH_TOKEN", "")

with open(PERSONA_PATH, encoding="utf-8") as _f:
    _persona = yaml.safe_load(_f)

AGENT_NAME: str = _persona["name"]
AGENT_DESC: str = _persona["description"]
AGENT_PORT: int = _persona.get("port", 9090)

SKILLS: list[AgentSkill] = []
CANNED_RESPONSES: dict[str, str] = {}

for _skill in _persona["skills"]:
    SKILLS.append(
        AgentSkill(
            id=_skill["id"],
            name=_skill["name"],
            description=_skill["description"],
            tags=_skill.get("tags", []),
            examples=_skill.get("examples", []),
            input_modes=["text/plain"],
            output_modes=["text/plain"],
        )
    )
    CANNED_RESPONSES[_skill["id"]] = _skill.get("response", "OK").strip()

DEFAULT_RESPONSE = "Stub agent processed your request successfully."

# ---------------------------------------------------------------------------
# Agent card
# ---------------------------------------------------------------------------


def _build_card() -> AgentCard:
    base_url = f"http://localhost:{AGENT_PORT}"
    return AgentCard(
        name=AGENT_NAME,
        description=AGENT_DESC,
        url=f"{base_url}/a2a",
        version="0.1.0",
        provider=AgentProvider(organization="Local Dev", url=base_url),
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            state_transition_history=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=SKILLS,
    )


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class StubExecutor(AgentExecutor):
    """Return canned responses keyed by skill_id."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        query = context.get_user_input()
        skill_id = context.metadata.get("skill_id", "") if context.metadata else ""

        logger.info(
            "[%s] task %s: skill=%s\n--- query ---\n%s\n--- end ---",
            AGENT_NAME,
            context.task_id,
            skill_id,
            query,
        )

        await updater.start_work()

        response_text = CANNED_RESPONSES.get(skill_id, DEFAULT_RESPONSE)
        await updater.add_artifact(
            [Part(root=TextPart(text=response_text))],
            name="response",
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token.

    The agent card endpoint is left open so the automator can discover
    skills before making authenticated RPC calls.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if AUTH_TOKEN and "/.well-known/" not in request.url.path:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {AUTH_TOKEN}":
                return Response("Unauthorized", status_code=401)
        return await call_next(request)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title=f"Stub A2A Agent — {AGENT_NAME}")
app.add_middleware(TokenAuthMiddleware)

agent_card = _build_card()
request_handler = DefaultRequestHandler(
    agent_executor=StubExecutor(),
    task_store=InMemoryTaskStore(),
    queue_manager=InMemoryQueueManager(),
)

a2a_app = A2AFastAPIApplication(
    agent_card=agent_card,
    http_handler=request_handler,
)
a2a_app.add_routes_to_app(
    app,
    agent_card_url="/.well-known/agent-card.json",
    rpc_url="/a2a",
)

print(
    f"Stub agent '{AGENT_NAME}' loaded with {len(SKILLS)} skills "
    f"from {PERSONA_PATH}",
    file=sys.stderr,
)
