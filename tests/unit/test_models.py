"""Unit tests for data models — validation, defaults, helpers."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.models import (
    AUTOMATIC,
    COMPLETED,
    MANUAL,
    AgentConfig,
    Event,
    PhaseConfig,
    Policy,
)


class TestPhaseConfig:
    def test_defaults(self):
        pc = PhaseConfig(name="assess")
        assert pc.mode == AUTOMATIC
        assert pc.operation is None

    def test_manual_mode(self):
        pc = PhaseConfig(name="approve", mode=MANUAL)
        assert pc.mode == MANUAL

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValidationError):
            PhaseConfig(name="bad", mode="invalid")

    def test_operation_set(self):
        pc = PhaseConfig(name="assess", operation="analyze this")
        assert pc.operation == "analyze this"


class TestPolicy:
    @staticmethod
    def _make(**overrides):
        defaults = dict(
            name="test",
            event_types=["alert"],
            phases=[PhaseConfig(name="process"), PhaseConfig(name=COMPLETED)],
        )
        defaults.update(overrides)
        return Policy(**defaults)

    def test_valid_construction(self):
        p = self._make()
        assert p.name == "test"
        assert len(p.phases) == 2

    def test_too_few_phases_rejected(self):
        with pytest.raises(ValidationError, match="at least two phases"):
            Policy(
                name="bad",
                event_types=["alert"],
                phases=[PhaseConfig(name=COMPLETED)],
            )

    def test_must_end_with_completed(self):
        with pytest.raises(ValidationError, match="must end with 'completed'"):
            Policy(
                name="bad",
                event_types=["alert"],
                phases=[PhaseConfig(name="a"), PhaseConfig(name="b")],
            )

    def test_multiple_event_types(self):
        p = self._make(event_types=["alert", "warning"])
        assert p.event_types == ["alert", "warning"]

    def test_first_phase(self):
        assert self._make().first_phase().name == "process"

    def test_next_phase(self):
        nxt = self._make().next_phase("process")
        assert nxt is not None
        assert nxt.name == COMPLETED

    def test_next_phase_at_end_returns_none(self):
        assert self._make().next_phase(COMPLETED) is None

    def test_next_phase_unknown_returns_none(self):
        assert self._make().next_phase("nonexistent") is None

    def test_get_phase_found(self):
        assert self._make().get_phase("process") is not None

    def test_get_phase_not_found(self):
        assert self._make().get_phase("nonexistent") is None


class TestAgentConfig:
    def test_defaults(self):
        a = AgentConfig(name="test", url="http://localhost:8080")
        assert a.timeout == 30
        assert a.headers is None

    def test_resolve_headers_explicit(self):
        a = AgentConfig(name="t", url="http://x", headers={"X-Custom": "val"})
        assert a.resolve_headers()["X-Custom"] == "val"

    def test_resolve_headers_env_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OLS_AUTOMATOR_AUTH_TOKEN", "env-tok")
        a = AgentConfig(name="t", url="http://x", token_path=str(tmp_path / "absent"))
        assert a.resolve_headers()["Authorization"] == "Bearer env-tok"

    def test_resolve_headers_env_takes_precedence_over_file(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("OLS_AUTOMATOR_AUTH_TOKEN", "env-tok")
        token_file = tmp_path / "token"
        token_file.write_text("file-tok")
        a = AgentConfig(name="t", url="http://x", token_path=str(token_file))
        assert a.resolve_headers()["Authorization"] == "Bearer env-tok"

    def test_resolve_headers_no_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OLS_AUTOMATOR_AUTH_TOKEN", raising=False)
        a = AgentConfig(name="t", url="http://x", token_path=str(tmp_path / "absent"))
        assert "Authorization" not in a.resolve_headers()

    def test_resolve_headers_reads_token_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OLS_AUTOMATOR_AUTH_TOKEN", raising=False)
        token_file = tmp_path / "token"
        token_file.write_text("my-token")
        a = AgentConfig(name="t", url="http://x", token_path=str(token_file))
        assert a.resolve_headers()["Authorization"] == "Bearer my-token"

    def test_explicit_auth_not_overridden(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("my-token")
        a = AgentConfig(
            name="t",
            url="http://x",
            headers={"Authorization": "Bearer explicit"},
            token_path=str(token_file),
        )
        assert a.resolve_headers()["Authorization"] == "Bearer explicit"


class TestEvent:
    def test_valid_event(self):
        e = Event(name="cpu-high", type="alert", content="x", ts=datetime.now())
        assert e.name == "cpu-high"

    def test_missing_fields_rejected(self):
        with pytest.raises(ValidationError):
            Event(name="incomplete")
