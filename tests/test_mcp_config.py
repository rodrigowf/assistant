"""Tests for utils.mcp_config — the unified MCP server loader.

Regression coverage for the case that prompted the rewrite: when a project
declares its MCPs in ``<project>/.mcp.json`` instead of the bundled CLI's
per-project map, the orchestrator used to advertise an empty list and the
voice model would hallucinate server names.
"""

from __future__ import annotations

import json

import pytest

from utils import mcp_config


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """Point :mod:`utils.mcp_config` at a clean tmp project root.

    Returns a helper that writes the two config files in the shapes the
    bundled Claude CLI uses on disk.
    """
    monkeypatch.setattr(mcp_config, "get_project_dir", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (tmp_path / ".claude_config").mkdir()

    def write(*, claude_json=None, project_mcp_json=None):
        if claude_json is not None:
            (tmp_path / ".claude_config" / ".claude.json").write_text(
                json.dumps(claude_json)
            )
        if project_mcp_json is not None:
            (tmp_path / ".mcp.json").write_text(json.dumps(project_mcp_json))

    return tmp_path, write


def test_loads_from_claude_json_project_map(isolated_project):
    project_dir, write = isolated_project
    write(claude_json={
        "projects": {
            str(project_dir): {
                "mcpServers": {"obs": {"type": "stdio", "command": "obs-mcp"}},
            },
        },
    })

    assert mcp_config.load_available_mcps() == {
        "obs": {"type": "stdio", "command": "obs-mcp"},
    }


def test_loads_from_project_mcp_json(isolated_project):
    """The original bug: ``.mcp.json`` was ignored entirely."""
    _, write = isolated_project
    write(project_mcp_json={
        "mcpServers": {
            "chrome-devtools": {"type": "stdio", "command": "npx"},
        },
    })

    assert mcp_config.load_available_mcps() == {
        "chrome-devtools": {"type": "stdio", "command": "npx"},
    }


def test_merges_both_sources(isolated_project):
    project_dir, write = isolated_project
    write(
        claude_json={
            "projects": {
                str(project_dir): {
                    "mcpServers": {"obs": {"type": "stdio", "command": "obs-mcp"}},
                },
            },
        },
        project_mcp_json={
            "mcpServers": {
                "chrome-devtools": {"type": "stdio", "command": "npx"},
            },
        },
    )

    assert set(mcp_config.load_available_mcps()) == {"obs", "chrome-devtools"}


def test_project_mcp_json_overrides_claude_json_on_collision(isolated_project):
    """``.mcp.json`` is the file the user edits, so it wins."""
    project_dir, write = isolated_project
    write(
        claude_json={
            "projects": {
                str(project_dir): {
                    "mcpServers": {"obs": {"command": "old-path"}},
                },
            },
        },
        project_mcp_json={"mcpServers": {"obs": {"command": "new-path"}}},
    )

    assert mcp_config.load_available_mcps()["obs"] == {"command": "new-path"}


def test_disabled_mcpjson_servers_are_filtered(isolated_project):
    project_dir, write = isolated_project
    write(
        claude_json={
            "projects": {
                str(project_dir): {
                    "mcpServers": {},
                    "disabledMcpjsonServers": ["chrome-devtools"],
                },
            },
        },
        project_mcp_json={
            "mcpServers": {
                "chrome-devtools": {"command": "npx"},
                "youtube": {"command": "youtube-mcp"},
            },
        },
    )

    available = mcp_config.load_available_mcps()
    assert "chrome-devtools" not in available
    assert "youtube" in available


def test_enabled_mcpjson_servers_is_whitelist(isolated_project):
    project_dir, write = isolated_project
    write(
        claude_json={
            "projects": {
                str(project_dir): {
                    "mcpServers": {},
                    "enabledMcpjsonServers": ["youtube"],
                },
            },
        },
        project_mcp_json={
            "mcpServers": {
                "chrome-devtools": {"command": "npx"},
                "youtube": {"command": "youtube-mcp"},
            },
        },
    )

    assert list(mcp_config.load_available_mcps()) == ["youtube"]


def test_get_mcp_configs_filters_to_requested(isolated_project):
    _, write = isolated_project
    write(project_mcp_json={
        "mcpServers": {
            "obs": {"command": "obs-mcp"},
            "chrome-devtools": {"command": "npx"},
        },
    })

    assert mcp_config.get_mcp_configs(["obs"]) == {"obs": {"command": "obs-mcp"}}


def test_get_mcp_configs_drops_unknown_names(isolated_project, caplog):
    """The hallucinated-name failure mode: returns only what exists, warns
    on the rest. ``open_agent_session`` uses this signal to error out
    instead of silently starting a session without the requested tools.
    """
    _, write = isolated_project
    write(project_mcp_json={"mcpServers": {"obs": {"command": "obs-mcp"}}})

    with caplog.at_level("WARNING", logger="utils.mcp_config"):
        result = mcp_config.get_mcp_configs(["obs", "google-banana"])

    assert result == {"obs": {"command": "obs-mcp"}}
    assert any("google-banana" in r.message for r in caplog.records)


def test_missing_files_return_empty(isolated_project):
    # Neither file written — should be a quiet empty dict, not a crash.
    assert mcp_config.load_available_mcps() == {}


def test_malformed_json_returns_empty(isolated_project, caplog):
    project_dir, _ = isolated_project
    (project_dir / ".mcp.json").write_text("{ not valid json")
    with caplog.at_level("WARNING", logger="utils.mcp_config"):
        assert mcp_config.load_available_mcps() == {}


def test_claude_config_dir_env_overrides_default(tmp_path, monkeypatch):
    """``CLAUDE_CONFIG_DIR`` should override the default
    ``.claude_config/`` location for the ``.claude.json`` lookup, matching
    the bundled CLI's behaviour."""
    monkeypatch.setattr(mcp_config, "get_project_dir", lambda: tmp_path)
    custom_dir = tmp_path / "alt-config"
    custom_dir.mkdir()
    (custom_dir / ".claude.json").write_text(json.dumps({
        "projects": {
            str(tmp_path): {"mcpServers": {"obs": {"command": "obs-mcp"}}},
        },
    }))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))

    assert mcp_config.load_available_mcps() == {"obs": {"command": "obs-mcp"}}
