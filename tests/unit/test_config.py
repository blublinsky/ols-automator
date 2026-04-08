"""Unit tests for AppConfig, YAML loading, and module-level accessors."""

import os
from unittest.mock import patch

import pytest
import yaml

import app.models.config as config_module
from app.models.config import AppConfig, get_config, load_config
from app.models.models import COMPLETED, PhaseConfig, Policy


class TestAppConfig:
    def test_default_database_url(self):
        cfg = AppConfig()
        assert "postgresql" in cfg.database_url

    def test_custom_database_url(self):
        cfg = AppConfig(database_url="sqlite+aiosqlite://")
        assert "sqlite" in cfg.database_url

    def test_engine_created(self):
        cfg = AppConfig(database_url="sqlite+aiosqlite://")
        assert cfg.engine is not None
        assert cfg.session_factory is not None

    def test_match_policy_found(self):
        policy = Policy(
            name="p",
            event_types=["alert", "warning"],
            phases=[PhaseConfig(name="step"), PhaseConfig(name=COMPLETED)],
        )
        cfg = AppConfig(database_url="sqlite+aiosqlite://", policies=[policy])
        assert cfg.match_policy("alert") is policy
        assert cfg.match_policy("warning") is policy

    def test_match_policy_not_found(self):
        cfg = AppConfig(database_url="sqlite+aiosqlite://", policies=[])
        assert cfg.match_policy("unknown") is None


class TestFromYaml:
    def test_valid_yaml(self, tmp_path):
        config = {
            "database_url": "sqlite+aiosqlite://",
            "policies": [
                {
                    "name": "test-policy",
                    "event_types": ["alert"],
                    "phases": [
                        {"name": "process", "mode": "automatic"},
                        {"name": "completed"},
                    ],
                }
            ],
            "agents": [
                {"name": "test-agent", "url": "http://localhost:9000"},
            ],
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(config))

        cfg = AppConfig.from_yaml(path)
        assert len(cfg.policies) == 1
        assert cfg.policies[0].name == "test-policy"
        assert len(cfg.agents) == 1
        assert cfg.agents[0].name == "test-agent"

    def test_env_overrides_database_url(self, tmp_path):
        config = {"database_url": "postgresql+asyncpg://original"}
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(config))

        with patch.dict(
            os.environ, {"OLS_AUTOMATOR_DATABASE_URL": "sqlite+aiosqlite://"}
        ):
            cfg = AppConfig.from_yaml(path)
        assert "sqlite" in cfg.database_url

    def test_invalid_policy_rejected(self, tmp_path):
        config = {
            "policies": [
                {
                    "name": "bad",
                    "event_types": ["alert"],
                    "phases": [{"name": "only-one"}],
                }
            ]
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(config))

        with pytest.raises(Exception):
            AppConfig.from_yaml(path)

    def test_empty_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("")
        cfg = AppConfig.from_yaml(path)
        assert cfg.policies == []
        assert cfg.agents == []


class TestLoadConfig:
    def setup_method(self):
        config_module._app_config = None

    def teardown_method(self):
        config_module._app_config = None

    def test_load_default(self):
        with patch.dict(os.environ, {}, clear=False):
            cfg = load_config()
        assert cfg is not None

    def test_load_from_yaml(self, tmp_path):
        config = {
            "database_url": "sqlite+aiosqlite://",
            "policies": [
                {
                    "name": "p",
                    "event_types": ["x"],
                    "phases": [{"name": "step"}, {"name": "completed"}],
                }
            ],
        }
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(config))

        cfg = load_config(path)
        assert len(cfg.policies) == 1

    def test_get_config_before_load_raises(self):
        with pytest.raises(RuntimeError, match="not loaded"):
            get_config()

    def test_get_config_after_load(self):
        load_config()
        assert get_config() is not None
