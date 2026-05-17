"""Tests for the orchestrator's get_assistant_config / update_assistant_config tools.

These tools wrap api.routes.config so the model can read and edit the
global config without hand-crafting JSON (which would skip Pydantic
validation and the working-directory id checks).  Coverage:

- get_assistant_config returns the current saved state.
- update_assistant_config persists deltas and returns the merged result.
- Validation errors surface as JSON ``{"error": ...}`` rather than
  raising — so a wrong field doesn't take down the voice turn.
- Unknown fields are rejected up front.
- A get → update → get round trip reflects the changes.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def isolated_project(tmp_path, monkeypatch):
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


def _local_entry(path: str) -> dict:
    return {
        "id": path, "path": path, "label": None,
        "ssh_host": None, "ssh_user": None, "ssh_key": None,
        "claude_config_dir": None,
    }


def _write_cfg(project_dir, data) -> None:
    (project_dir / "assistant_config.json").write_text(json.dumps(data))


async def _call(tool_name, **kwargs):
    """Invoke a registered tool by name with no shared context."""
    from orchestrator.tools import registry
    import orchestrator.tools.assistant_config  # noqa: F401

    return await registry.execute(tool_name, kwargs, context={})


async def test_get_returns_current_config(isolated_project):
    entry = _local_entry(str(isolated_project))
    _write_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
        "chrome_extension": True,
    })

    result = json.loads(await _call("get_assistant_config"))
    assert result["working_directory"] == entry["id"]
    assert result["chrome_extension"] is True


async def test_update_persists_and_returns_merged(isolated_project):
    entry_a = _local_entry(str(isolated_project / "a"))
    entry_b = _local_entry(str(isolated_project / "b"))
    (isolated_project / "a").mkdir()
    (isolated_project / "b").mkdir()
    _write_cfg(isolated_project, {
        "working_directory": entry_a["id"],
        "working_directory_history": [entry_a, entry_b],
        "chrome_extension": False,
    })

    updated = json.loads(await _call(
        "update_assistant_config",
        working_directory=entry_b["id"],
        chrome_extension=True,
    ))
    assert updated["working_directory"] == entry_b["id"]
    assert updated["chrome_extension"] is True

    # Re-read from disk to make sure it actually persisted.
    fresh = json.loads(await _call("get_assistant_config"))
    assert fresh["working_directory"] == entry_b["id"]
    assert fresh["chrome_extension"] is True


async def test_update_with_unknown_working_directory_id_returns_error(isolated_project):
    entry = _local_entry(str(isolated_project))
    _write_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
    })

    result = json.loads(await _call(
        "update_assistant_config",
        working_directory="/does/not/exist",
    ))
    assert "error" in result
    assert "/does/not/exist" in result["error"]


async def test_update_with_unknown_field_returns_error(isolated_project):
    entry = _local_entry(str(isolated_project))
    _write_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
    })

    result = json.loads(await _call(
        "update_assistant_config",
        nonsense_field="value",
    ))
    assert "error" in result


async def test_update_with_no_fields_returns_error(isolated_project):
    entry = _local_entry(str(isolated_project))
    _write_cfg(isolated_project, {
        "working_directory": entry["id"],
        "working_directory_history": [entry],
    })

    result = json.loads(await _call("update_assistant_config"))
    assert "error" in result
    assert "No fields supplied" in result["error"]
