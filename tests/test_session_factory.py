"""Tests for api.session_factory.build_session_config.

This factory is the shared resolution path between the UI's "+" button
(api.routes.chat) and the orchestrator's open_agent_session tool, so
regressions here would silently let the orchestrator drift from the UI's
behaviour — exactly the bug this module was extracted to prevent.

Coverage:
- Working directory resolution: local entry, SSH entry, per-session
  override, stale per-session id fallback.
- Provider + harness model precedence (re-uses the same precedence rules
  test_chat_provider_resolution.py covers — these tests only confirm
  build_session_config wires them through).
- Chrome extension flag from session vs. global.
- MCP resolution: caller override (incl. explicit empty list),
  per-session enabled_mcps, global enabled_mcps.
- Fresh re-read on every call (the bug the orchestrator hit: it used a
  process-start snapshot and never noticed UI edits).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
    """Point every config loader at a clean tmp project root.

    Each module that consumes ``utils.paths`` imports its helpers at load
    time, binding the originals; monkeypatching the module attribute
    doesn't reach those already-bound references.  So we rebind each
    module's local copy as well.
    """
    from utils import paths as paths_mod
    from utils import mcp_config
    from api.routes import config as cfg_mod
    from api.routes import session_config as sess_cfg_mod

    monkeypatch.setattr(paths_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(paths_mod, "get_project_dir", lambda: tmp_path)
    monkeypatch.setattr(paths_mod, "get_context_dir", lambda: tmp_path / "context")
    (tmp_path / "context").mkdir()

    monkeypatch.setattr(mcp_config, "get_project_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(sess_cfg_mod, "get_context_dir", lambda: tmp_path / "context")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    return tmp_path


def _write_assistant_cfg(project_dir: Path, data: dict) -> None:
    (project_dir / "assistant_config.json").write_text(json.dumps(data))


def _write_mcp_json(project_dir: Path, mcps: dict[str, dict]) -> None:
    (project_dir / ".mcp.json").write_text(json.dumps({"mcpServers": mcps}))


def _write_session_cfg(project_dir: Path, sdk_id: str, data: dict) -> None:
    (project_dir / "context" / f"{sdk_id}.config.json").write_text(json.dumps(data))


def _local_entry(path: str) -> dict:
    return {
        "id": path, "path": path, "label": None,
        "ssh_host": None, "ssh_user": None, "ssh_key": None,
        "claude_config_dir": None,
    }


def _ssh_entry(host: str, user: str, path: str, key: str = "/keys/id_rsa") -> dict:
    return {
        "id": f"{host}:{path}", "path": path, "label": None,
        "ssh_host": host, "ssh_user": user, "ssh_key": key,
        "claude_config_dir": path + "/.claude_config",
    }


def test_returns_local_working_directory(isolated_project):
    _write_assistant_cfg(isolated_project, {
        "working_directory": "/data/proj",
        "working_directory_history": [_local_entry("/data/proj")],
    })

    from api.session_factory import build_session_config
    config, mcps, info = build_session_config()

    assert config.project_dir == "/data/proj"
    assert config.ssh_host is None
    assert config.ssh_user is None
    assert config.ssh_key is None
    assert config.ssh_claude_config_dir is None
    assert info["project_dir"] == "/data/proj"
    assert info["ssh_host"] is None


def test_ssh_entry_propagates_all_ssh_fields(isolated_project):
    """The original bug: orchestrator sessions silently ran locally even
    when the UI was pointed at an SSH host."""
    entry = _ssh_entry("jetson.lan", "rodrigo", "/home/rodrigo/assistant")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
    })

    from api.session_factory import build_session_config
    config, _, info = build_session_config()

    assert config.project_dir == "/home/rodrigo/assistant"
    assert config.ssh_host == "jetson.lan"
    assert config.ssh_user == "rodrigo"
    assert config.ssh_key == "/keys/id_rsa"
    assert config.ssh_claude_config_dir == "/home/rodrigo/assistant/.claude_config"
    assert info["ssh_host"] == "jetson.lan"


def test_per_session_working_directory_overrides_global(isolated_project):
    local_entry = _local_entry("/data/proj")
    remote_entry = _ssh_entry("box.lan", "alice", "/srv/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": local_entry["id"],
        "working_directory_history": [local_entry, remote_entry],
    })
    _write_session_cfg(isolated_project, "sess123", {
        "working_directory": remote_entry["id"],
    })

    from api.session_factory import build_session_config
    config, _, _ = build_session_config(resume_sdk_id="sess123")

    assert config.ssh_host == "box.lan"
    assert config.project_dir == "/srv/proj"


def test_stale_session_working_directory_falls_back_to_global(isolated_project):
    """A per-session id that no longer exists in history shouldn't crash —
    fall back to the global active entry."""
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
    })
    _write_session_cfg(isolated_project, "sess123", {
        "working_directory": "/no/longer/exists",
    })

    from api.session_factory import build_session_config
    config, _, _ = build_session_config(resume_sdk_id="sess123")

    assert config.project_dir == "/data/proj"


def test_chrome_flag_from_global(isolated_project):
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "chrome_extension": True,
    })

    from api.session_factory import build_session_config
    config, _, info = build_session_config()

    assert config.extra_args == {"chrome": None}
    assert info["chrome_extension"] is True


def test_session_chrome_overrides_global(isolated_project):
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "chrome_extension": True,
    })
    _write_session_cfg(isolated_project, "sess123", {"chrome_extension": False})

    from api.session_factory import build_session_config
    config, _, info = build_session_config(resume_sdk_id="sess123")

    assert config.extra_args is None
    assert info["chrome_extension"] is False


def test_mcp_override_replaces_global(isolated_project):
    """Caller's explicit list wins over the global enabled_mcps."""
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "enabled_mcps": ["obs"],
    })
    _write_mcp_json(isolated_project, {
        "obs": {"command": "obs-mcp"},
        "chrome-devtools": {"command": "npx"},
    })

    from api.session_factory import build_session_config
    _, mcps, info = build_session_config(mcp_override=["chrome-devtools"])

    assert set(mcps or {}) == {"chrome-devtools"}
    assert info["mcp_servers"] == ["chrome-devtools"]


def test_empty_mcp_override_is_honoured(isolated_project):
    """An explicit empty list means 'no MCPs for this session' — distinct
    from None ('inherit')."""
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "enabled_mcps": ["obs"],
    })
    _write_mcp_json(isolated_project, {"obs": {"command": "obs-mcp"}})

    from api.session_factory import build_session_config
    _, mcps, info = build_session_config(mcp_override=[])

    assert mcps is None
    assert info["mcp_servers"] == []


def test_none_mcp_override_inherits_global(isolated_project):
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "enabled_mcps": ["obs"],
    })
    _write_mcp_json(isolated_project, {"obs": {"command": "obs-mcp"}})

    from api.session_factory import build_session_config
    _, mcps, _ = build_session_config(mcp_override=None)

    assert set(mcps or {}) == {"obs"}


def test_session_enabled_mcps_overrides_global(isolated_project):
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "enabled_mcps": ["obs"],
    })
    _write_mcp_json(isolated_project, {
        "obs": {"command": "obs-mcp"},
        "chrome-devtools": {"command": "npx"},
    })
    _write_session_cfg(isolated_project, "sess123", {
        "enabled_mcps": ["chrome-devtools"],
    })

    from api.session_factory import build_session_config
    _, mcps, _ = build_session_config(resume_sdk_id="sess123")

    assert set(mcps or {}) == {"chrome-devtools"}


def test_provider_and_model_propagate_from_global(isolated_project):
    entry = _local_entry("/data/proj")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "provider": "claude",
        "harness_model": {"claude": "claude-sonnet-4-5-20250929"},
    })

    from api.session_factory import build_session_config
    config, _, info = build_session_config()

    assert config.provider == "claude"
    assert config.model == "claude-sonnet-4-5-20250929"
    assert info["provider"] == "claude"
    assert info["model"] == "claude-sonnet-4-5-20250929"


def test_each_call_re_reads_config_from_disk(isolated_project):
    """The headline regression: if you edit the config and immediately
    spawn a session, the session must see the new config — not a snapshot."""
    entry_a = _local_entry("/proj/a")
    entry_b = _local_entry("/proj/b")
    _write_assistant_cfg(isolated_project, {
        "working_directory": entry_a["id"],
        "working_directory_history": [entry_a, entry_b],
    })

    from api.session_factory import build_session_config
    config1, _, _ = build_session_config()
    assert config1.project_dir == "/proj/a"

    _write_assistant_cfg(isolated_project, {
        "working_directory": entry_b["id"],
        "working_directory_history": [entry_a, entry_b],
    })

    config2, _, _ = build_session_config()
    assert config2.project_dir == "/proj/b"
