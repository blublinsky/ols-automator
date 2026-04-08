"""A2A client — invoke remote agents directly without LangChain."""

import logging
from typing import Any
from uuid import uuid4

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.middleware import ClientCallContext, ClientCallInterceptor
from a2a.types import (
    AgentCard,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TextPart,
)

logger = logging.getLogger(__name__)


class _HeaderInterceptor(ClientCallInterceptor):
    """Inject authorization headers into every outbound A2A RPC call."""

    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = headers

    async def intercept(
        self,
        method_name: str,
        request_payload: dict[str, Any],
        http_kwargs: dict[str, Any],
        agent_card: AgentCard | None,
        context: ClientCallContext | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add stored headers to the HTTP request kwargs."""
        existing = http_kwargs.get("headers", {})
        existing.update(self._headers)
        http_kwargs["headers"] = existing
        return request_payload, http_kwargs


async def fetch_agent_card(
    base_url: str,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
) -> AgentCard:
    """Fetch an A2A agent card from its well-known endpoint."""
    async with httpx.AsyncClient(
        headers=headers or {}, timeout=httpx.Timeout(timeout)
    ) as http:
        resolver = A2ACardResolver(http, base_url)
        return await resolver.get_agent_card()


def _extract_task_text(task: Task) -> str:
    """Extract text content from a completed A2A Task.

    Prefers artifact text; falls back to the status message.
    """
    parts: list[str] = []

    if task.artifacts:
        for artifact in task.artifacts:
            parts.extend(
                part.root.text
                for part in artifact.parts
                if isinstance(part.root, TextPart)
            )

    if parts:
        return "\n".join(parts)

    if task.status and task.status.message:
        parts.extend(
            part.root.text
            for part in task.status.message.parts
            if isinstance(part.root, TextPart)
        )

    return "\n".join(parts) if parts else ""


async def send_message(
    card: AgentCard,
    text: str,
    headers: dict[str, str] | None = None,
    skill_id: str | None = None,
) -> str:
    """Send a text message to an A2A agent and return the response text."""
    metadata: dict[str, str] = {}
    if skill_id:
        metadata["skill_id"] = skill_id

    message = Message(
        message_id=uuid4().hex,
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        metadata=metadata or None,
    )

    interceptors: list[ClientCallInterceptor] = []
    if headers:
        interceptors.append(_HeaderInterceptor(headers))

    client = ClientFactory(ClientConfig(streaming=False)).create(
        card, interceptors=interceptors
    )

    try:
        async for event in client.send_message(message):
            if isinstance(event, tuple):
                task, _ = event
                if task.status.state == TaskState.failed:
                    error = (
                        _extract_task_text(task) or "Remote agent returned an error."
                    )
                    raise RuntimeError(error)
                return _extract_task_text(task)

            if isinstance(event, Message):
                return "\n".join(
                    part.root.text
                    for part in event.parts
                    if isinstance(part.root, TextPart)
                )
    finally:
        await client.close()  # type: ignore[attr-defined]

    return ""
