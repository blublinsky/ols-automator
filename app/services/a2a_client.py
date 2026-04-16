"""A2A client — invoke remote agents directly without LangChain."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from uuid import uuid4

import httpx
from a2a.client.card_resolver import A2ACardResolver
from a2a.client.client import ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.client.errors import A2AClientHTTPError, A2AClientTimeoutError
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

from app.models.models import DEFAULT_INVOCATION_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# Fixed ceiling for agent-card HTTP fetch at startup (not workflow send_message).
_AGENT_CARD_FETCH_TIMEOUT_SECONDS = 30

_RETRIABLE_HTTP_STATUS = frozenset({502, 503, 504})

# Shared exponential backoff for transient HTTP/A2A failures (card fetch + send_message).
_TRANSIENT_RETRY_MAX_EXTRA = 3
_TRANSIENT_RETRY_BACKOFF_BASE_SECONDS = 0.5

T = TypeVar("T")


def transient_invocation_error(exc: BaseException) -> bool:
    """True if the error may be worth retrying (timeouts, transport, 502/503/504)."""
    if isinstance(exc, A2AClientTimeoutError):
        return True
    if isinstance(exc, A2AClientHTTPError):
        return exc.status_code in _RETRIABLE_HTTP_STATUS
    if isinstance(exc, httpx.TimeoutException):
        return True
    if isinstance(exc, (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)):
        return True
    return False


async def _async_retry_on_transient(
    op_label: str,
    attempt_fn: Callable[[], Awaitable[T]],
    *,
    max_extra_retries: int,
    backoff_base_seconds: float,
    is_transient: Callable[[BaseException], bool] = transient_invocation_error,
) -> T:
    """Run ``attempt_fn`` until success or give up; sleep exponentially between transient failures."""
    max_attempts = 1 + max_extra_retries
    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await attempt_fn()
        except BaseException as e:
            last_error = e
            if not is_transient(e) or attempt >= max_attempts:
                raise
            delay = backoff_base_seconds * (2 ** (attempt - 1))
            logger.warning(
                "Transient error in %s (attempt %d/%d): %s; retrying in %.2fs",
                op_label,
                attempt,
                max_attempts,
                e,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


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
    timeout: int = _AGENT_CARD_FETCH_TIMEOUT_SECONDS,
) -> AgentCard:
    """Fetch an A2A agent card from its well-known endpoint."""
    logger.info("Fetching agent card from %s", base_url)

    async def _attempt() -> AgentCard:
        async with httpx.AsyncClient(
            headers=headers or {}, timeout=httpx.Timeout(timeout)
        ) as http:
            resolver = A2ACardResolver(http, base_url)
            return await resolver.get_agent_card()

    card = await _async_retry_on_transient(
        f"fetch_agent_card({base_url})",
        _attempt,
        max_extra_retries=_TRANSIENT_RETRY_MAX_EXTRA,
        backoff_base_seconds=_TRANSIENT_RETRY_BACKOFF_BASE_SECONDS,
    )
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


async def _send_message_once(
    card: AgentCard,
    message: Message,
    interceptors: list[ClientCallInterceptor],
    request_metadata: dict[str, str] | None,
    timeout_seconds: float,
) -> str:
    """One A2A client lifecycle: open transport, stream ``send_message``, close."""
    http_timeout = httpx.Timeout(timeout_seconds)
    http_client = httpx.AsyncClient(timeout=http_timeout)
    client = ClientFactory(
        ClientConfig(streaming=False, httpx_client=http_client)
    ).create(card, interceptors=interceptors)

    try:
        async for event in client.send_message(
            message, request_metadata=request_metadata
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


async def send_message(
    card: AgentCard,
    text: str,
    headers: dict[str, str] | None = None,
    skill_id: str | None = None,
    *,
    timeout_seconds: float = DEFAULT_INVOCATION_TIMEOUT_SECONDS,
) -> str:
    """Send a text message to an A2A agent and return the response text.

    ``timeout_seconds`` is applied to the outbound HTTP client (connect, read,
    write, pool) for each attempt. Transient failures (timeouts, transport
    errors, HTTP 502/503/504) are retried with exponential backoff; see
    ``_TRANSIENT_RETRY_MAX_EXTRA`` and ``_TRANSIENT_RETRY_BACKOFF_BASE_SECONDS``.
    """
    request_metadata: dict[str, str] | None = None
    if skill_id:
        request_metadata = {"skill_id": skill_id}

    interceptors: list[ClientCallInterceptor] = []
    if headers:
        interceptors.append(_HeaderInterceptor(headers))

    max_attempts = 1 + _TRANSIENT_RETRY_MAX_EXTRA
    logger.info(
        "Sending message to %s (skill=%s, timeout=%ss, max_attempts=%s)",
        card.name,
        skill_id or "none",
        timeout_seconds,
        max_attempts,
    )

    async def _attempt() -> str:
        message = Message(
            message_id=uuid4().hex,
            role=Role.user,
            parts=[Part(root=TextPart(text=text))],
        )
        return await _send_message_once(
            card,
            message,
            interceptors,
            request_metadata,
            timeout_seconds,
        )

    return await _async_retry_on_transient(
        f"send_message({card.name})",
        _attempt,
        max_extra_retries=_TRANSIENT_RETRY_MAX_EXTRA,
        backoff_base_seconds=_TRANSIENT_RETRY_BACKOFF_BASE_SECONDS,
    )
