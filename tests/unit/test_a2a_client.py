"""Unit tests for the A2A client — card fetching, text extraction, messaging."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)

from app.services.a2a_client import (
    _HeaderInterceptor,
    _extract_task_text,
    fetch_agent_card,
    send_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AsyncIterFromList:
    """Wrap a list as an async iterator (for mocking client.send_message)."""

    def __init__(self, items):
        self._items = iter(items)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._items)
        except StopIteration:
            raise StopAsyncIteration


def _make_card(**overrides) -> AgentCard:
    defaults = dict(
        name="test-agent",
        description="A test agent",
        url="http://localhost:9090/a2a",
        version="0.1.0",
        capabilities=AgentCapabilities(),
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
        skills=[
            AgentSkill(
                id="skill-1",
                name="Test Skill",
                description="A test skill",
                tags=["test"],
            )
        ],
    )
    defaults.update(overrides)
    return AgentCard(**defaults)


def _make_task(state=TaskState.completed, artifact_text=None, status_text=None):
    artifacts = None
    if artifact_text:
        artifacts = [
            Artifact(
                artifactId="a1",
                parts=[Part(root=TextPart(text=artifact_text))],
            )
        ]
    status_msg = None
    if status_text:
        status_msg = Message(
            messageId="m1",
            role=Role.agent,
            parts=[Part(root=TextPart(text=status_text))],
        )
    return Task(
        id="t1",
        contextId="ctx1",
        status=TaskStatus(state=state, message=status_msg),
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# _HeaderInterceptor
# ---------------------------------------------------------------------------


class TestHeaderInterceptor:
    @pytest.mark.asyncio
    async def test_injects_headers(self):
        interceptor = _HeaderInterceptor({"Authorization": "Bearer tok"})
        _, http_kwargs = await interceptor.intercept("send_message", {}, {}, None, None)
        assert http_kwargs["headers"]["Authorization"] == "Bearer tok"

    @pytest.mark.asyncio
    async def test_merges_with_existing(self):
        interceptor = _HeaderInterceptor({"X-Custom": "val"})
        _, http_kwargs = await interceptor.intercept(
            "send_message", {}, {"headers": {"Accept": "text/plain"}}, None, None
        )
        assert http_kwargs["headers"]["X-Custom"] == "val"
        assert http_kwargs["headers"]["Accept"] == "text/plain"


# ---------------------------------------------------------------------------
# _extract_task_text
# ---------------------------------------------------------------------------


class TestExtractTaskText:
    def test_from_artifacts(self):
        task = _make_task(artifact_text="result from artifact")
        assert _extract_task_text(task) == "result from artifact"

    def test_falls_back_to_status_message(self):
        task = _make_task(status_text="status fallback")
        assert _extract_task_text(task) == "status fallback"

    def test_artifacts_preferred_over_status(self):
        task = _make_task(artifact_text="artifact wins", status_text="ignored")
        assert _extract_task_text(task) == "artifact wins"

    def test_empty_when_no_text(self):
        task = _make_task()
        assert _extract_task_text(task) == ""


# ---------------------------------------------------------------------------
# fetch_agent_card
# ---------------------------------------------------------------------------


class TestFetchAgentCard:
    @pytest.mark.asyncio
    async def test_returns_card(self):
        card = _make_card()
        with patch("app.services.a2a_client.A2ACardResolver") as mock_resolver_cls:
            mock_resolver_cls.return_value.get_agent_card = AsyncMock(return_value=card)
            result = await fetch_agent_card("http://localhost:9090")

        assert result.name == "test-agent"
        assert len(result.skills) == 1


# ---------------------------------------------------------------------------
# send_message
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_returns_task_artifact_text(self):
        card = _make_card()
        task = _make_task(artifact_text="agent output")

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([(task, None)])
        mock_client.close = AsyncMock()

        with patch("app.services.a2a_client.ClientFactory") as factory_cls:
            factory_cls.return_value.create.return_value = mock_client
            result = await send_message(card, "hello")

        assert result == "agent output"
        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_message_text(self):
        card = _make_card()
        msg = Message(
            messageId="m1",
            role=Role.agent,
            parts=[Part(root=TextPart(text="message response"))],
        )

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([msg])
        mock_client.close = AsyncMock()

        with patch("app.services.a2a_client.ClientFactory") as factory_cls:
            factory_cls.return_value.create.return_value = mock_client
            result = await send_message(card, "hello")

        assert result == "message response"

    @pytest.mark.asyncio
    async def test_raises_on_failed_task(self):
        card = _make_card()
        task = _make_task(state=TaskState.failed, artifact_text="error detail")

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([(task, None)])
        mock_client.close = AsyncMock()

        with (
            patch("app.services.a2a_client.ClientFactory") as factory_cls,
            pytest.raises(RuntimeError, match="error detail"),
        ):
            factory_cls.return_value.create.return_value = mock_client
            await send_message(card, "hello")

        mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_passes_skill_id_in_request_metadata(self):
        card = _make_card()
        task = _make_task(artifact_text="ok")

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([(task, None)])
        mock_client.close = AsyncMock()

        with patch("app.services.a2a_client.ClientFactory") as factory_cls:
            factory_cls.return_value.create.return_value = mock_client
            await send_message(card, "hello", skill_id="my-skill")

        call_kwargs = mock_client.send_message.call_args
        assert call_kwargs.kwargs["request_metadata"] == {"skill_id": "my-skill"}

    @pytest.mark.asyncio
    async def test_no_metadata_without_skill_id(self):
        card = _make_card()
        task = _make_task(artifact_text="ok")

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([(task, None)])
        mock_client.close = AsyncMock()

        with patch("app.services.a2a_client.ClientFactory") as factory_cls:
            factory_cls.return_value.create.return_value = mock_client
            await send_message(card, "hello")

        call_kwargs = mock_client.send_message.call_args
        assert call_kwargs.kwargs["request_metadata"] is None

    @pytest.mark.asyncio
    async def test_header_interceptor_attached(self):
        card = _make_card()
        task = _make_task(artifact_text="ok")

        mock_client = MagicMock()
        mock_client.send_message.return_value = _AsyncIterFromList([(task, None)])
        mock_client.close = AsyncMock()

        with patch("app.services.a2a_client.ClientFactory") as factory_cls:
            mock_create = factory_cls.return_value.create
            mock_create.return_value = mock_client
            await send_message(card, "hello", headers={"Authorization": "Bearer t"})

        interceptors = mock_create.call_args.kwargs["interceptors"]
        assert len(interceptors) == 1
        assert isinstance(interceptors[0], _HeaderInterceptor)
