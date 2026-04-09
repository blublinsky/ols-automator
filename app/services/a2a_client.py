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
    logger.info("Fetching agent card from %s", base_url)
    async with httpx.AsyncClient(
        headers=headers or {}, timeout=httpx.Timeout(timeout)
    ) as http:
        resolver = A2ACardResolver(http, base_url)
        card = await resolver.get_agent_card()
    skill_count = len(card.skills) if card.skills else 0
    logger.info("Discovered agent '%s' (%d skills)", card.name, skill_count)
    return card


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
    request_metadata: dict[str, str] = {}
    if skill_id:
        request_metadata["skill_id"] = skill_id

    message = Message(
        message_id=uuid4().hex,
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
    )

    interceptors: list[ClientCallInterceptor] = []
    if headers:
        interceptors.append(_HeaderInterceptor(headers))

    client = ClientFactory(ClientConfig(streaming=False)).create(
        card, interceptors=interceptors
    )

    logger.info("Sending message to %s (skill=%s)", card.name, skill_id or "none")

    try:
        async for event in client.send_message(
            message, request_metadata=request_metadata or None
        ):
            if isinstance(event, tuple):
                task, _ = event
                if task.status.state == TaskState.failed:
                    error = (
                        _extract_task_text(task) or "Remote agent returned an error."
                    )
                    raise RuntimeError(error)
                response = _extract_task_text(task)
                logger.info(
                    "Received response from %s (%d chars)",
                    card.name,
                    len(response),
                )
                return response

            if isinstance(event, Message):
                response = "\n".join(
                    part.root.text
                    for part in event.parts
                    if isinstance(part.root, TextPart)
                )
                logger.info(
                    "Received message from %s (%d chars)",
                    card.name,
                    len(response),
                )
                return response
    finally:
        await client.close()  # type: ignore[attr-defined]

    return ""
