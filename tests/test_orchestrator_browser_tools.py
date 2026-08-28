"""Tests for orchestrator/tools/browser.py — the agent-facing browser tools."""

from __future__ import annotations

import json

import pytest

from api.routes.browser import (
    BrowserCommandError,
    BrowserCommandTimeout,
    BrowserNotConnected,
)
from orchestrator.tools import browser as browser_tools


class FakeHub:
    """Stand-in for BrowserHub that records commands and replays canned results."""

    def __init__(self, results=None, connected=True, raises=None):
        self.connected = connected
        self.calls = []
        self._results = results or {}
        self._raises = raises or {}

    async def send_command(self, command, params=None, timeout=None):
        self.calls.append((command, params, timeout))
        if command in self._raises:
            raise self._raises[command]
        return self._results.get(command, {"ok": True})


def _ctx(hub):
    return {"browser_hub": hub}


SNAPSHOT = {
    "url": "https://example.com",
    "title": "Example",
    "generation": 3,
    "truncated": False,
    "viewport": {"cssWidth": 1440, "cssHeight": 900, "devicePixelRatio": 2},
    "markdown": "# Example\n[button] Go {ref=e1, selector=#go}",
    "elements": [{"ref": "e1", "selector": "#go", "role": "button", "name": "Go"}],
}

SHOT = {"width": 2880, "height": 1800, "uploadUrl": "/uploads/x.jpg"}


# ---------------------------------------------------------------------------
# browser_look
# ---------------------------------------------------------------------------

async def test_look_returns_snapshot_and_screenshot():
    hub = FakeHub({"snapshot": SNAPSHOT, "capture_screenshot": SHOT})
    out = json.loads(await browser_tools.browser_look(context=_ctx(hub)))

    assert out["markdown"].startswith("# Example")
    assert out["elements"][0]["ref"] == "e1"
    assert out["generation"] == 3
    assert out["screenshot"]["uploadUrl"] == "/uploads/x.jpg"

    # Both halves of "look" are fetched, in one tool call.
    assert [c[0] for c in hub.calls] == ["snapshot", "capture_screenshot"]


async def test_look_can_skip_the_screenshot():
    hub = FakeHub({"snapshot": SNAPSHOT})
    out = json.loads(await browser_tools.browser_look(context=_ctx(hub), screenshot=False))

    assert "screenshot" not in out
    assert [c[0] for c in hub.calls] == ["snapshot"]


async def test_look_survives_a_failed_screenshot():
    """The text snapshot is the more actionable half — a screenshot failure
    must not throw it away."""
    hub = FakeHub(
        {"snapshot": SNAPSHOT},
        raises={"capture_screenshot": BrowserCommandError("quota exceeded")},
    )
    out = json.loads(await browser_tools.browser_look(context=_ctx(hub)))

    assert out["markdown"].startswith("# Example")
    assert "quota exceeded" in out["screenshot_error"]
    assert "error" not in out


async def test_look_forwards_max_chars():
    hub = FakeHub({"snapshot": SNAPSHOT, "capture_screenshot": SHOT})
    await browser_tools.browser_look(context=_ctx(hub), max_chars=5000)

    assert hub.calls[0][1] == {"maxChars": 5000}


# ---------------------------------------------------------------------------
# Availability vs failure — the agent should react differently to each
# ---------------------------------------------------------------------------

async def test_disconnected_extension_reports_browser_unavailable():
    out = json.loads(await browser_tools.browser_look(context=_ctx(FakeHub(connected=False))))

    assert out["error"] == "browser_unavailable"
    assert "extension" in out["detail"]


async def test_missing_hub_reports_browser_unavailable():
    out = json.loads(await browser_tools.browser_look(context={}))
    assert out["error"] == "browser_unavailable"


async def test_command_error_reports_command_failed():
    hub = FakeHub(raises={"click": BrowserCommandError("no_match: #nope")})
    out = json.loads(await browser_tools.browser_click(context=_ctx(hub), selector="#nope"))

    assert out["error"] == "command_failed"
    assert "no_match" in out["detail"]


async def test_timeout_reports_command_failed():
    hub = FakeHub(raises={"click": BrowserCommandTimeout("timed out after 30s")})
    out = json.loads(await browser_tools.browser_click(context=_ctx(hub), selector="#x"))

    assert out["error"] == "command_failed"
    assert "timeout" in out["detail"]


async def test_disconnect_mid_command_is_unavailable_not_failure():
    hub = FakeHub(raises={"click": BrowserNotConnected("extension disconnected")})
    out = json.loads(await browser_tools.browser_click(context=_ctx(hub), selector="#x"))

    assert out["error"] == "browser_unavailable"


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------

async def test_click_prefers_ref_over_selector():
    hub = FakeHub()
    await browser_tools.browser_click(context=_ctx(hub), ref="e1", selector="#go")

    assert hub.calls[0][1] == {"ref": "e1"}


async def test_click_by_position_forwards_generation():
    hub = FakeHub()
    await browser_tools.browser_click(
        context=_ctx(hub), position={"x": 0.5, "y": 0.25}, generation=3
    )

    assert hub.calls[0][1] == {"position": {"x": 0.5, "y": 0.25}, "generation": 3}


async def test_click_without_a_target_is_rejected():
    hub = FakeHub()
    out = json.loads(await browser_tools.browser_click(context=_ctx(hub)))

    assert out["error"] == "command_failed"
    assert "target is required" in out["detail"]
    assert hub.calls == []  # nothing sent to the browser


async def test_fill_forwards_value():
    hub = FakeHub()
    await browser_tools.browser_fill(context=_ctx(hub), ref="e1", value="hello")

    assert hub.calls[0][1] == {"ref": "e1", "value": "hello"}


async def test_fill_forwards_checkbox_state():
    hub = FakeHub()
    await browser_tools.browser_fill(context=_ctx(hub), selector="#tos", checked=True)

    assert hub.calls[0][1] == {"selector": "#tos", "checked": True}


async def test_fill_allows_empty_string_value():
    """Clearing a field is a real operation; '' must not be dropped as falsy."""
    hub = FakeHub()
    await browser_tools.browser_fill(context=_ctx(hub), ref="e1", value="")

    assert hub.calls[0][1] == {"ref": "e1", "value": ""}


# ---------------------------------------------------------------------------
# Navigation, tabs, scroll
# ---------------------------------------------------------------------------

async def test_navigate_forwards_url():
    hub = FakeHub({"navigate": {"tabId": 1, "url": "https://example.com"}})
    out = json.loads(await browser_tools.browser_navigate(
        context=_ctx(hub), url="https://example.com"
    ))

    assert hub.calls[0][0] == "navigate"
    assert hub.calls[0][1] == {"url": "https://example.com"}
    assert out["tabId"] == 1


async def test_tabs_lists_tabs_and_windows():
    hub = FakeHub({"list_tabs": {"tabs": [{"tabId": 1}], "windows": [{"windowId": 9}]}})
    out = json.loads(await browser_tools.browser_tabs(context=_ctx(hub)))

    assert out["tabs"][0]["tabId"] == 1
    assert out["windows"][0]["windowId"] == 9


async def test_switch_tab_forwards_tab_id():
    hub = FakeHub()
    await browser_tools.browser_switch_tab(context=_ctx(hub), tab_id=7)

    assert hub.calls[0][1] == {"tabId": 7}


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"to": "bottom"}, {"to": "bottom"}),
        ({"pages": 1.5}, {"pages": 1.5}),
        ({"pages": -1}, {"pages": -1}),
        ({"selector": "#footer"}, {"selector": "#footer"}),
    ],
)
async def test_scroll_variants(kwargs, expected):
    hub = FakeHub()
    await browser_tools.browser_scroll(context=_ctx(hub), **kwargs)

    assert hub.calls[0][1] == expected


async def test_scroll_without_arguments_is_rejected():
    hub = FakeHub()
    out = json.loads(await browser_tools.browser_scroll(context=_ctx(hub)))

    assert out["error"] == "command_failed"
    assert hub.calls == []


# ---------------------------------------------------------------------------
# execute_js
# ---------------------------------------------------------------------------

async def test_execute_js_forwards_code_and_world():
    hub = FakeHub({"execute_js": {"world": "MAIN", "result": 42}})
    out = json.loads(await browser_tools.browser_execute_js(
        context=_ctx(hub), code="return 42;", world="MAIN"
    ))

    assert hub.calls[0][1]["code"] == "return 42;"
    assert hub.calls[0][1]["world"] == "MAIN"
    assert out["result"] == 42


async def test_execute_js_converts_timeout_to_milliseconds():
    hub = FakeHub({"execute_js": {}})
    await browser_tools.browser_execute_js(context=_ctx(hub), code="return 1;", timeout=5)

    command, params, transport_timeout = hub.calls[0]
    assert params["timeout"] == 5000
    # Transport waits longer than the page, so the page's own error wins.
    assert transport_timeout > 5


async def test_execute_js_surfaces_page_errors():
    hub = FakeHub(raises={"execute_js": BrowserCommandError("boom")})
    out = json.loads(await browser_tools.browser_execute_js(context=_ctx(hub), code="throw 1"))

    assert out["error"] == "command_failed"
    assert "boom" in out["detail"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_all_browser_tools_are_registered():
    from orchestrator.tools import registry

    names = set(registry.tool_names)
    assert {
        "browser_look", "browser_navigate", "browser_tabs", "browser_switch_tab",
        "browser_click", "browser_fill", "browser_scroll", "browser_execute_js",
    } <= names


def test_tool_schemas_are_valid_json_schema_objects():
    from orchestrator.tools import registry

    for definition in registry.get_definitions():
        if not definition["name"].startswith("browser_"):
            continue
        schema = definition["input_schema"]
        assert schema["type"] == "object"
        assert isinstance(schema.get("properties", {}), dict)


def test_look_description_teaches_the_snapshot_first_loop():
    """The delegation logic is only enforced by what the descriptions say."""
    from orchestrator.tools import registry

    definitions = {d["name"]: d["description"] for d in registry.get_definitions()}

    assert "first" in definitions["browser_look"].lower()
    for name in ("browser_click", "browser_fill"):
        assert "browser_look" in definitions[name]
    assert "fallback" in definitions["browser_click"].lower()
