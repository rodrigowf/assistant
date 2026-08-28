#!/usr/bin/env python3
"""Live end-to-end check against the real Chrome extension.

Runs the *production* code paths in-process — `api.routes.browser` for the
transport and `orchestrator.tools.browser` for the agent-facing tools — against
whatever browser is currently attached. Not a unit test: this needs a real
Chrome with the extension loaded and configured.

    context/scripts/run.sh browser-extension/tools/live_check.py recon
    context/scripts/run.sh browser-extension/tools/live_check.py full

`recon` is strictly read-only. `full` navigates the active tab to local fixture
pages and restores the original URL afterwards.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
from fastapi import FastAPI

from api.routes import browser as browser_route
from api.routes import uploads as uploads_route
from orchestrator.tools import browser as browser_tools

FIXTURES = "http://127.0.0.1:8899/test-fixtures"

# python -m http.server sends Last-Modified with no Cache-Control, so Chrome
# heuristically caches the fixtures and can serve a stale copy after an edit.
CACHE_BUST = str(int(time.time()))


def fixture(name: str) -> str:
    return f"{FIXTURES}/{name}?cb={CACHE_BUST}"

results: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {label}"
    if detail:
        line += f" — {detail[:300]}"
    print(line, flush=True)
    return ok


def build_app() -> FastAPI:
    """Minimal app: the real browser router, none of the heavy lifespan.

    The uploads router is included too — the extension POSTs screenshots to
    /api/uploads on the origin it derives from its WebSocket URL, which is this
    harness. Without it every capture 404s at the upload step.
    """
    app = FastAPI()
    app.state.browser_hub = browser_route.BrowserHub()
    app.include_router(browser_route.router)
    app.include_router(uploads_route.router)
    return app


async def wait_for_extension(hub, timeout: float = 60.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if hub.connected:
            return True
        await asyncio.sleep(0.5)
    return False


async def tool(fn, **kwargs) -> dict:
    """Call an orchestrator tool and decode its JSON-string contract."""
    return json.loads(await fn(**kwargs))


async def recon(hub) -> dict:
    ctx = {"browser_hub": hub}
    print("\n== Recon (read-only) ==", flush=True)

    pong = await hub.send_command("ping", {}, timeout=10)
    record("ping round-trip", bool(pong.get("pong")), str(pong))

    tabs = await tool(browser_tools.browser_tabs, context=ctx)
    ok = "tabs" in tabs and "windows" in tabs
    record("browser_tabs lists tabs + windows", ok,
           f"{len(tabs.get('tabs', []))} tabs, {len(tabs.get('windows', []))} windows")

    active = next((t for t in tabs.get("tabs", []) if t.get("active")), None)
    record("active tab identified", active is not None,
           f"{active.get('url') if active else 'none'}")

    look = await tool(browser_tools.browser_look, context=ctx)
    if "error" in look:
        detail = look.get("detail", "")
        # chrome:// pages and the Web Store are unscriptable by design, so an
        # active tab parked on one is a skip, not a failure.
        if "restricted page" in detail:
            print(f"  [SKIP] browser_look — active tab is unscriptable ({detail[:80]})",
                  flush=True)
        else:
            record("browser_look", False, detail)
        return {"active": active}

    record("browser_look returns markdown", bool(look.get("markdown")),
           f"{len(look.get('markdown') or '')} chars")
    record("browser_look returns elements", isinstance(look.get("elements"), list),
           f"{len(look.get('elements') or [])} elements")

    shot = look.get("screenshot")
    if shot:
        dpr = shot.get("devicePixelRatio")
        expected_w = round(shot["viewportCssWidth"] * dpr)
        record("screenshot captured + uploaded", bool(shot.get("uploadUrl")),
               f"{shot['width']}x{shot['height']} dpr={dpr} url={shot.get('uploadUrl')}")
        record("screenshot width == cssWidth * devicePixelRatio",
               abs(shot["width"] - expected_w) <= 1,
               f"{shot['width']} vs {expected_w}")
    else:
        record("screenshot captured", False, look.get("screenshot_error", "missing"))

    return {"active": active}


async def full(hub, original_url: str | None) -> None:
    ctx = {"browser_hub": hub}

    # --- snapshot fidelity on a known page ---------------------------
    print("\n== Snapshot + interaction (fixture pages) ==", flush=True)
    nav = await tool(browser_tools.browser_navigate, context=ctx,
                     url=fixture("article.html"))
    record("navigate to fixture", "article.html" in nav.get("url", ""), str(nav.get("url")))

    look = await tool(browser_tools.browser_look, context=ctx)
    md = look.get("markdown", "")
    record("markdown has heading", "# Main Heading" in md)
    record("markdown excludes hidden text", "must not appear" not in md)
    els = look.get("elements", [])
    submit = next((e for e in els if e.get("selector") == "#submit-btn"), None)
    record("button captured with id selector", submit is not None)
    record("disabled state captured",
           any(e.get("state", {}).get("disabled") for e in els))
    boxes_ok = all(0 <= e["box"]["x"] <= 1 and 0 <= e["box"]["y"] <= 1 for e in els)
    record("bounding boxes are proportions in [0,1]", boxes_ok)

    # --- click / fill against the controlled-form fixture -------------
    await tool(browser_tools.browser_navigate, context=ctx, url=fixture("react-form.html"))
    look = await tool(browser_tools.browser_look, context=ctx, screenshot=False)
    els = look.get("elements", [])

    email = next((e for e in els if e.get("role") == "input:text"), None)
    record("text input found in snapshot", email is not None,
           f"selector={email.get('selector') if email else None}")
    record("selector avoids hashed class",
           bool(email) and "css-" not in (email.get("selector") or ""),
           email.get("selector") if email else "")

    filled = await tool(browser_tools.browser_fill, context=ctx,
                        ref=email["ref"], value="live@example.com")
    record("browser_fill by ref", "error" not in filled, str(filled)[:200])

    # The real proof is framework state, not the DOM value — but read it back
    # through the page's #state-mirror rather than execute_js, so this doesn't
    # depend on the injection feature it isn't testing.
    async def mirror() -> str:
        snap = await tool(browser_tools.browser_look, context=ctx, screenshot=False)
        for line in (snap.get("markdown") or "").splitlines():
            if line.startswith("state:"):
                return line
        return ""

    state_line = await mirror()
    record("React value-tracker defeated (component state updated)",
           "live@example.com" in state_line, state_line[:200])

    # A ref from before mirror()'s re-snapshot must now fail loudly rather than
    # silently resolving to whatever element occupies that slot.
    stale_btn = next((e for e in els if e.get("role") == "button"), None)
    stale = await tool(browser_tools.browser_click, context=ctx, ref=stale_btn["ref"])
    record("stale ref rejected after re-snapshot",
           stale.get("error") == "command_failed" and "unknown_ref" in stale.get("detail", ""),
           stale.get("detail", "")[:160])

    fresh = await tool(browser_tools.browser_look, context=ctx, screenshot=False)
    btn = next((e for e in fresh.get("elements", []) if e.get("role") == "button"), None)
    clicked = await tool(browser_tools.browser_click, context=ctx, ref=btn["ref"])
    record("browser_click by fresh ref", "error" not in clicked, str(clicked)[:160])
    state_line = await mirror()
    record("click actually fired the page handler",
           '"submitted":true' in state_line.replace(" ", ""), state_line[:200])

    # --- scroll + stale-viewport --------------------------------------
    print("\n== Scroll + stale-viewport ==", flush=True)
    await tool(browser_tools.browser_navigate, context=ctx,
               url=fixture("shadow-and-scroll.html"))
    look = await tool(browser_tools.browser_look, context=ctx, screenshot=False)
    gen = look.get("generation")
    els = look.get("elements", [])

    shadow = next((e for e in els if e.get("name") == "Shadow Button"), None)
    record("open shadow-root element captured", shadow is not None)
    record("shadow element has no document selector",
           bool(shadow) and shadow.get("selector") is None)

    pos_ok = await tool(browser_tools.browser_click, context=ctx,
                        position={"x": 0.5, "y": 0.5}, generation=gen)
    record("position targeting works pre-scroll", "error" not in pos_ok, str(pos_ok)[:120])

    scrolled = await tool(browser_tools.browser_scroll, context=ctx, to="bottom")
    record("scroll bumps generation", scrolled.get("generation", 0) > gen,
           f"{gen} -> {scrolled.get('generation')}")

    stale = await tool(browser_tools.browser_click, context=ctx,
                       position={"x": 0.5, "y": 0.5}, generation=gen)
    record("stale position rejected after scroll",
           stale.get("error") == "command_failed" and "stale_viewport" in stale.get("detail", ""),
           stale.get("detail", "")[:160])

    by_sel = await tool(browser_tools.browser_click, context=ctx, selector="#bottom-btn")
    record("selector targeting immune to scroll drift", "error" not in by_sel)

    # --- execute_js: the CSP spike ------------------------------------
    print("\n== execute_js (CSP spike) ==", flush=True)
    r = await tool(browser_tools.browser_execute_js, context=ctx, code="return 1 + 1;")
    record("execute_js basic", r.get("result") == 2, str(r)[:160])
    record("execute_js ran in MAIN world", r.get("world") == "MAIN", str(r.get("world")))

    r = await tool(browser_tools.browser_execute_js, context=ctx,
                   code="window.__liveProbe = 'set-by-agent'; return window.__liveProbe;")
    record("execute_js reaches page globals (MAIN world)",
           r.get("result") == "set-by-agent", str(r)[:160])

    r = await tool(browser_tools.browser_execute_js, context=ctx,
                   code="return document.querySelector('#bottom-btn');")
    el = r.get("result") or {}
    record("DOM node serialised with usable selector",
           el.get("__element") is True and el.get("selector") is not None, str(el)[:160])

    r = await tool(browser_tools.browser_execute_js, context=ctx, code="throw new Error('deliberate');")
    record("execute_js error captured", r.get("error") == "command_failed"
           and "deliberate" in r.get("detail", ""), r.get("detail", "")[:120])

    # Strict-CSP site — the open question from PLAN Phase 6.
    print("\n-- strict-CSP site (github.com) --", flush=True)
    nav = await tool(browser_tools.browser_navigate, context=ctx, url="https://github.com")
    if "error" in nav:
        record("navigate to github.com", False, nav.get("detail", ""))
    else:
        record("navigate to github.com", True, nav.get("url", ""))
        r = await tool(browser_tools.browser_execute_js, context=ctx,
                       code="return document.title;")
        ok = "error" not in r
        record("execute_js on strict-CSP site", ok,
               f"world={r.get('world')} result={r.get('result')!r}" if ok else r.get("detail", "")[:200])
        if ok:
            record("CSP VERDICT: MAIN-world injection allowed under strict CSP",
                   r.get("world") == "MAIN",
                   f"world used = {r.get('world')}")

    # --- restore --------------------------------------------------------
    if original_url and original_url.startswith(("http://", "https://")):
        back = await tool(browser_tools.browser_navigate, context=ctx, url=original_url)
        record("original tab URL restored", "error" not in back, original_url)


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "recon"

    app = build_app()
    hub = app.state.browser_hub
    config = uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="error")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())

    print("Waiting for the extension to connect…", flush=True)
    if not await wait_for_extension(hub):
        print("TIMEOUT: no extension connected within 60s", flush=True)
        server.should_exit = True
        await serve_task
        return 2
    print(f"Extension attached: {hub.status()['client']}", flush=True)

    try:
        info = await recon(hub)
        if mode == "full":
            original = (info.get("active") or {}).get("url")
            await full(hub, original)
    finally:
        server.should_exit = True
        await serve_task

    passed = sum(1 for _, ok, _ in results if ok)
    failed = [r for r in results if not r[1]]
    print(f"\n=== {passed} passed, {len(failed)} failed ===", flush=True)
    for label, _, detail in failed:
        print(f"  FAILED: {label} — {detail[:200]}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
