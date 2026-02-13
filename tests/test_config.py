"""Tests for manager/config.py."""

import json

import pytest

from manager.config import ManagerConfig


class TestDefaults:
    def test_default_project_dir(self):
        config = ManagerConfig()
        assert config.project_dir.endswith("assistant")

    def test_default_permission_mode(self):
        config = ManagerConfig()
        assert config.permission_mode == "default"

    def test_default_model_is_none(self):
        config = ManagerConfig()
        assert config.model is None

    def test_default_budget_is_none(self):
        config = ManagerConfig()
        assert config.max_budget_usd is None


class TestLoadFromFile:
    def test_load_from_json(self, tmp_path):
        cfg_file = tmp_path / ".manager.json"
        cfg_file.write_text(json.dumps({
            "model": "sonnet",
            "max_budget_usd": 5.0,
            "max_turns": 10,
        }))

        config = ManagerConfig.load(cfg_file)
        assert config.model == "sonnet"
        assert config.max_budget_usd == 5.0
        assert config.max_turns == 10

    def test_missing_file_uses_defaults(self, tmp_path):
        config = ManagerConfig.load(tmp_path / "nonexistent.json")
        assert config.model is None
        assert config.permission_mode == "default"

    def test_ignores_unknown_fields(self, tmp_path):
        cfg_file = tmp_path / ".manager.json"
        cfg_file.write_text(json.dumps({
            "model": "opus",
            "unknown_field": "value",
        }))

        config = ManagerConfig.load(cfg_file)
        assert config.model == "opus"
        assert not hasattr(config, "unknown_field")


class TestLoadFromEnv:
    def test_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("MANAGER_MODEL", "haiku")
        # Use a nonexistent file so only env applies
        config = ManagerConfig.load("/nonexistent/.manager.json")
        assert config.model == "haiku"

    def test_env_overrides_file(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / ".manager.json"
        cfg_file.write_text(json.dumps({"model": "sonnet"}))

        monkeypatch.setenv("MANAGER_MODEL", "opus")
        config = ManagerConfig.load(cfg_file)
        # Env takes precedence over file
        assert config.model == "opus"

    def test_env_budget_coerced_to_float(self, monkeypatch):
        monkeypatch.setenv("MANAGER_MAX_BUDGET_USD", "3.5")
        config = ManagerConfig.load("/nonexistent/.manager.json")
        assert config.max_budget_usd == 3.5
        assert isinstance(config.max_budget_usd, float)

    def test_env_max_turns_coerced_to_int(self, monkeypatch):
        monkeypatch.setenv("MANAGER_MAX_TURNS", "25")
        config = ManagerConfig.load("/nonexistent/.manager.json")
        assert config.max_turns == 25
        assert isinstance(config.max_turns, int)
