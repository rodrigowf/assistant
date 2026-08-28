"""Browser-control tools — drive Rodrigo's real logged-in Chrome.

These proxy to the Chrome extension in ``browser-extension/`` over the
WebSocket in ``api/routes/browser.py``. Commands always act on the **active
tab**; use ``browser_switch_tab`` to reach another one.

The intended loop (PLAN Phase 7, approved 2026-08-27):

1. ``browser_look`` first — screenshot *and* text snapshot together.
2. Target by ``ref`` or ``selector`` from that snapshot whenever possible.
3. ``position`` is a fallback, and is rejected once the page scrolls.

Tool descriptions below restate that, because the failure mode is an agent
guessing selectors it never saw or reusing coordinates after a scroll.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Snapshotting a heavy page is DOM-walk bound; injected JS can await network.
_SNAPSHOT_TIMEOUT = 45.0
_DEFAULT_TIMEOUT = 30.0


def _hub(context: dict[str, Any]):
    """Return the BrowserHub, or None when the backend wasn't wired for it."""
    return context.get("browser_hub")


async def _call(
    context: dict[str, Any],
    command: str,
    params: dict[str, Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Any:
    """Send a command, translating transport failures into tool-level errors.

    Raises ``_BrowserUnavailable`` for "no browser" so callers can distinguish
    it from a command that ran and failed — the agent should react differently
    to a disconnected extension than to a bad selector.
    """
    from api.routes.browser import (
        BrowserCommandError,
        BrowserCommandTimeout,
        BrowserNotConnected,
    )

    hub = _hub(context)
    if hub is None:
        raise _BrowserUnavailable(
            "browser control is not wired into this backend (no browser_hub in context)"
        )
    if not hub.connected:
        raise _BrowserUnavailable(
            "no browser extension connected — is Chrome running with the "
            "Archie Browser Control extension loaded and configured?"
        )

    try:
        return await hub.send_command(command, params or {}, timeout=timeout)
    except BrowserNotConnected as e:
        raise _BrowserUnavailable(str(e)) from e
    except BrowserCommandTimeout as e:
        raise _BrowserFailed(f"timeout: {e}") from e
    except BrowserCommandError as e:
        raise _BrowserFailed(str(e)) from e


class _BrowserUnavailable(RuntimeError):
    """The extension isn't reachable."""


class _BrowserFailed(RuntimeError):
    """The extension ran the command and it failed."""


def _wrap(fn):
    """Turn tool bodies into the JSON-string contract the registry expects."""
    async def wrapper(*args, **kwargs) -> str:
        try:
            return json.dumps(await fn(*args, **kwargs), default=str)
        except _BrowserUnavailable as e:
            return json.dumps({"error": "browser_unavailable", "detail": str(e)})
        except _BrowserFailed as e:
            return json.dumps({"error": "command_failed", "detail": str(e)})
        except Exception as e:  # noqa: BLE001
            logger.exception("browser tool %s failed", fn.__name__)
            return json.dumps({"error": "unexpected", "detail": str(e)})

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    # The registry inspects the signature to filter tool_input, so it has to
    # be the wrapped function's, not (*args, **kwargs).
    import inspect
    wrapper.__signature__ = inspect.signature(fn)
    return wrapper


_TARGET_PROPS = {
    "ref": {
        "type": "string",
        "description": (
            "Element ref from the most recent browser_look snapshot (e.g. 'e12'). "
            "PREFERRED — refs are exact and survive pages whose class names are "
            "build-generated hashes."
        ),
    },
    "selector": {
        "type": "string",
        "description": (
            "CSS selector. Use the 'selector' field from a browser_look snapshot "
            "rather than inventing one."
        ),
    },
    "position": {
        "type": "object",
        "description": (
            "FALLBACK ONLY. Proportional viewport coordinates, both in [0,1], "
            "relative to the screenshot. Rejected with 'stale_viewport' if the "
            "page scrolled since the snapshot — re-run browser_look first."
        ),
        "properties": {
            "x": {"type": "number"},
            "y": {"type": "number"},
        },
    },
    "generation": {
        "type": "integer",
        "description": "Snapshot generation the position came from. Send it with 'position'.",
    },
}


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------

@registry.register(
    name="browser_look",
    description=(
        "LOOK AT THE PAGE. Captures a screenshot AND a markdown text snapshot of "
        "the active tab in one call. ALWAYS call this first, before any click, "
        "fill or scroll — it is how you see the page, and it produces the 'ref' "
        "and 'selector' values every other browser tool needs. The snapshot lists "
        "every visible interactive element (buttons, links, inputs, menus) with a "
        "ref, a CSS selector, its state, and its position. Re-run it after "
        "anything that changes the page, including scrolling."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": (
                    "Markdown budget, default 40000. A content-heavy page can "
                    "exceed this and come back truncated; the element list is "
                    "never truncated."
                ),
            },
            "screenshot": {
                "type": "boolean",
                "description": "Include the screenshot. Default true.",
            },
        },
    },
)
@_wrap
async def browser_look(
    context: dict,
    max_chars: int | None = None,
    screenshot: bool = True,
) -> dict:
    params: dict[str, Any] = {}
    if max_chars:
        params["maxChars"] = max_chars

    snap = await _call(context, "snapshot", params, timeout=_SNAPSHOT_TIMEOUT)

    out: dict[str, Any] = {
        "url": snap.get("url"),
        "title": snap.get("title"),
        "generation": snap.get("generation"),
        "truncated": snap.get("truncated"),
        "viewport": snap.get("viewport"),
        "markdown": snap.get("markdown"),
        "elements": snap.get("elements"),
    }

    if screenshot:
        # A failed screenshot must not discard a good snapshot — the text is
        # the more actionable half.
        try:
            out["screenshot"] = await _call(context, "capture_screenshot", {})
        except (_BrowserFailed, _BrowserUnavailable) as e:
            out["screenshot_error"] = str(e)

    return out


# --------------------------------------------------------------------------
# Navigation and tabs
# --------------------------------------------------------------------------

@registry.register(
    name="browser_navigate",
    description=(
        "Navigate the active tab to a URL and wait for it to finish loading. "
        "Follow with browser_look to see the new page."
    ),
    input_schema={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute URL to open."}},
        "required": ["url"],
    },
)
@_wrap
async def browser_navigate(context: dict, url: str) -> dict:
    return await _call(context, "navigate", {"url": url}, timeout=_SNAPSHOT_TIMEOUT)


@registry.register(
    name="browser_tabs",
    description=(
        "List every open tab and window. Use it to find the tabId to pass to "
        "browser_switch_tab. All other browser tools act on the ACTIVE tab only."
    ),
    input_schema={"type": "object", "properties": {}},
)
@_wrap
async def browser_tabs(context: dict) -> dict:
    return await _call(context, "list_tabs", {})


@registry.register(
    name="browser_switch_tab",
    description=(
        "Make a different tab active. Required before acting on any tab other "
        "than the current one, since every other browser tool targets the "
        "active tab. Follow with browser_look."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tab_id": {"type": "integer", "description": "tabId from browser_tabs."},
        },
        "required": ["tab_id"],
    },
)
@_wrap
async def browser_switch_tab(context: dict, tab_id: int) -> dict:
    return await _call(context, "switch_tab", {"tabId": tab_id})


# --------------------------------------------------------------------------
# Interaction
# --------------------------------------------------------------------------

@registry.register(
    name="browser_click",
    description=(
        "Click an element in the active tab. Target it by 'ref' or 'selector' "
        "from the most recent browser_look — do not guess a selector. 'position' "
        "is a fallback for things the snapshot can't name, and is rejected if the "
        "page scrolled since the snapshot."
    ),
    input_schema={"type": "object", "properties": dict(_TARGET_PROPS)},
)
@_wrap
async def browser_click(
    context: dict,
    ref: str | None = None,
    selector: str | None = None,
    position: dict | None = None,
    generation: int | None = None,
) -> dict:
    return await _call(context, "click", _target(ref, selector, position, generation))


@registry.register(
    name="browser_fill",
    description=(
        "Fill a form field in the active tab: text inputs, textareas, selects, "
        "checkboxes and radios. Works correctly with React/Vue controlled "
        "components. Target by 'ref' or 'selector' from browser_look. For a "
        "select, 'value' matches either the option value or its visible label."
    ),
    input_schema={
        "type": "object",
        "properties": {
            **_TARGET_PROPS,
            "value": {"type": "string", "description": "Text, or the option to select."},
            "checked": {
                "type": "boolean",
                "description": "Desired state for a checkbox or radio.",
            },
        },
    },
)
@_wrap
async def browser_fill(
    context: dict,
    ref: str | None = None,
    selector: str | None = None,
    position: dict | None = None,
    generation: int | None = None,
    value: str | None = None,
    checked: bool | None = None,
) -> dict:
    params = _target(ref, selector, position, generation)
    if value is not None:
        params["value"] = value
    if checked is not None:
        params["checked"] = checked
    return await _call(context, "fill", params)


@registry.register(
    name="browser_scroll",
    description=(
        "Scroll the active tab. Give exactly one of: 'to' ('top'/'bottom'), "
        "'pages' (viewport heights, negative scrolls up), or a 'ref'/'selector' "
        "to scroll into view. Scrolling invalidates every position coordinate "
        "from the last snapshot, so run browser_look again afterwards."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "to": {"type": "string", "enum": ["top", "bottom"]},
            "pages": {"type": "number", "description": "Viewport heights; negative scrolls up."},
            "ref": _TARGET_PROPS["ref"],
            "selector": _TARGET_PROPS["selector"],
        },
    },
)
@_wrap
async def browser_scroll(
    context: dict,
    to: str | None = None,
    pages: float | None = None,
    ref: str | None = None,
    selector: str | None = None,
) -> dict:
    params: dict[str, Any] = {}
    if to:
        params["to"] = to
    if pages is not None:
        params["pages"] = pages
    if ref:
        params["ref"] = ref
    if selector:
        params["selector"] = selector
    if not params:
        raise _BrowserFailed("browser_scroll needs one of: to, pages, ref, selector")
    return await _call(context, "scroll", params)


# --------------------------------------------------------------------------
# Escape hatch
# --------------------------------------------------------------------------

@registry.register(
    name="browser_execute_js",
    description=(
        "Run arbitrary JavaScript in the active tab, like typing into the "
        "DevTools console. The code is an async function body — use 'return' to "
        "send a value back, and 'await' freely. Runs in the page's main world by "
        "default, so page globals and framework internals are reachable. Use it "
        "for anything the dedicated tools don't cover; prefer browser_click / "
        "browser_fill for ordinary interaction. DOM elements come back as "
        "descriptors carrying a CSS selector you can reuse."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "JS to run. Async function body; use 'return' for a result.",
            },
            "world": {
                "type": "string",
                "enum": ["MAIN", "USER_SCRIPT"],
                "description": (
                    "MAIN (default) shares the page's JS context. USER_SCRIPT is "
                    "isolated from page globals but exempt from the page's CSP; "
                    "MAIN falls back to it automatically if CSP blocks execution."
                ),
            },
            "timeout": {"type": "number", "description": "Seconds, default 15."},
        },
        "required": ["code"],
    },
)
@_wrap
async def browser_execute_js(
    context: dict,
    code: str,
    world: str | None = None,
    timeout: float | None = None,
) -> dict:
    params: dict[str, Any] = {"code": code}
    if world:
        params["world"] = world
    if timeout:
        params["timeout"] = int(timeout * 1000)
    # Give the transport headroom over the in-page timeout so the page's own
    # error surfaces instead of a transport timeout masking it.
    return await _call(context, "execute_js", params, timeout=(timeout or 15.0) + 10.0)


def _target(
    ref: str | None,
    selector: str | None,
    position: dict | None,
    generation: int | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if ref:
        params["ref"] = ref
    elif selector:
        params["selector"] = selector
    elif position:
        params["position"] = position
        if generation is not None:
            params["generation"] = generation
    else:
        raise _BrowserFailed(
            "a target is required: ref or selector (preferred) or position. "
            "Run browser_look first to get them."
        )
    return params
