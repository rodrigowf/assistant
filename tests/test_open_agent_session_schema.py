"""Tests for the dynamic schema + error-result handling around
``open_agent_session``.

Two regressions covered:

1. The tool's ``mcp_servers`` schema must carry an ``enum`` populated from
   the live MCP list — this is what keeps the model from passing made-up
   names like ``"google-banana"`` and getting the call rejected by the
   orchestrator instead of by the schema validator.

2. ``api.routes.orchestrator._tool_result_is_error`` must flip the
   broadcast's ``is_error`` flag when a tool returns
   ``json.dumps({"error": "..."})`` — the route layer used to hardcode
   ``is_error: False`` so failures rendered as green "done" bubbles.
"""

from __future__ import annotations

import json

import pytest

from api.routes.orchestrator import _tool_result_is_error
from orchestrator.tools import registry
import orchestrator.tools.agent_sessions  # noqa: F401  — triggers registration
from utils import mcp_config


@pytest.fixture
def isolated_mcps(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_config, "get_project_dir", lambda: tmp_path)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    def install(mcps: dict[str, dict]):
        (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": mcps}))

    return install


def _open_agent_session_schema() -> dict:
    """Pull the live-rendered schema the way the providers see it."""
    for definition in registry.get_definitions():
        if definition["name"] == "open_agent_session":
            return definition["input_schema"]
    raise AssertionError("open_agent_session not registered")


def test_schema_enum_reflects_available_mcps(isolated_mcps):
    isolated_mcps({"chrome-devtools": {"command": "npx"}, "obs": {"command": "obs-mcp"}})

    schema = _open_agent_session_schema()
    items = schema["properties"]["mcp_servers"]["items"]

    assert items["type"] == "string"
    assert sorted(items["enum"]) == ["chrome-devtools", "obs"]
    # Description should also list the live names so the model can read
    # them even on providers that don't surface enum constraints.
    description = schema["properties"]["mcp_servers"]["description"]
    assert "chrome-devtools" in description
    assert "obs" in description


def test_schema_enum_empty_when_no_mcps_configured(isolated_mcps):
    isolated_mcps({})

    schema = _open_agent_session_schema()
    items = schema["properties"]["mcp_servers"]["items"]

    assert items == {"type": "string", "enum": []}
    assert "No MCP servers" in schema["properties"]["mcp_servers"]["description"]


def test_schema_is_rebuilt_each_call(isolated_mcps):
    """Live state should not be cached at registration time."""
    isolated_mcps({"obs": {"command": "obs-mcp"}})
    first = _open_agent_session_schema()
    assert first["properties"]["mcp_servers"]["items"]["enum"] == ["obs"]

    isolated_mcps({"chrome-devtools": {"command": "npx"}})
    second = _open_agent_session_schema()
    assert second["properties"]["mcp_servers"]["items"]["enum"] == ["chrome-devtools"]


def test_schema_builder_does_not_mutate_static_input(isolated_mcps):
    """Each render must yield an independent schema dict so registry state
    can't leak across calls."""
    isolated_mcps({"obs": {"command": "obs-mcp"}})
    s1 = _open_agent_session_schema()
    s1["properties"]["mcp_servers"]["items"]["enum"].append("hacked")

    s2 = _open_agent_session_schema()
    assert "hacked" not in s2["properties"]["mcp_servers"]["items"]["enum"]


@pytest.mark.parametrize(
    "output, expected",
    [
        (json.dumps({"error": "boom"}), True),
        (json.dumps({"session_id": "abc", "status": "started"}), False),
        ("", False),
        ("plain text not json", False),
        (json.dumps([1, 2, 3]), False),
        (json.dumps({"error": None}), True),  # presence of key, not truthiness
    ],
)
def test_tool_result_is_error_detection(output, expected):
    assert _tool_result_is_error(output) is expected
