#!/usr/bin/env python3
"""Live end-to-end check against the real Chrome extension.

Runs the *production* transport in-process (`api.routes.browser`) against
whatever browser is currently attached. Not a unit test: this needs a real
Chrome with the extension loaded and configured.

    context/scripts/run.sh browser-extension/tools/live_check.py recon
    context/scripts/run.sh browser-extension/tools/live_check.py full

`recon` is strictly read-only. `full` navigates the active tab to local fixture
pages (served from browser-extension/ on :8899) and restores the original URL.

Note this binds :8765 itself, so stop the tunnel first if one is running —
otherwise it tests against the Jetson rather than its own hub.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn
from fastapi import FastAPI

from api.routes import browser as browser_route
from api.routes import uploads as uploads_route
from api.routes.browser import BrowserCommandError, BrowserCommandTimeout

FIXTURES = "http://127.0.0.1:8899/test-fixtures"

# python -m http.server sends Last-Modified with no Cache-Control, so Chrome
# heuristically caches the fixtures and can serve a stale copy after an edit.
CACHE_BUST = str(int(time.time()))


def fixture(name: str) -> str:
    return f"{FIXTURES}/{name}?cb={CACHE_BUST}"


results: list[tuple[str, bool, str]] = []


def record(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, ok, detail))
    line = f"  [{'PASS' if ok else 'FAIL'}] {label}"
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if hub.connected:
            return True
        await asyncio.sleep(0.5)
    return False


async def send(hub, command: str, params: dict | None = None, timeout: float = 30.0):
    """Send a command, returning ``(result, error_string)``."""
    try:
        return await hub.send_command(command, params or {}, timeout=timeout), None
    except (BrowserCommandError, BrowserCommandTimeout) as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


async def look(hub, screenshot: bool = True) -> dict:
    """Mirror what the `look` subcommand does: snapshot, then screenshot."""
    snap, err = await send(hub, "snapshot", {}, timeout=90)
    if err:
        return {"error": err}
    if screenshot:
        shot, shot_err = await send(hub, "capture_screenshot", {}, timeout=60)
        # A failed screenshot must not discard a good snapshot.
        snap["screenshot"] = shot
        snap["screenshot_error"] = shot_err
    return snap


async def recon(hub) -> dict:
    print("\n== Recon (read-only) ==", flush=True)

    pong, err = await send(hub, "ping", {}, timeout=10)
    record("ping round-trip", bool(pong and pong.get("pong")), err or str(pong))

    tabs, err = await send(hub, "list_tabs")
    ok = bool(tabs) and "tabs" in tabs and "windows" in tabs
    record("list_tabs returns tabs + windows", ok,
           err or f"{len(tabs.get('tabs', []))} tabs, {len(tabs.get('windows', []))} windows")

    active = next((t for t in (tabs or {}).get("tabs", []) if t.get("active")), None)
    record("active tab identified", active is not None,
           str(active.get("url") if active else "none"))

    snap = await look(hub)
    if snap.get("error"):
        detail = snap["error"]
        # chrome:// pages and the Web Store are unscriptable by design, so an
        # active tab parked on one is a skip, not a failure.
        if "restricted page" in detail:
            print(f"  [SKIP] look — active tab is unscriptable ({detail[:80]})", flush=True)
        else:
            record("look", False, detail)
        return {"active": active}

    record("look returns markdown", bool(snap.get("markdown")),
           f"{len(snap.get('markdown') or '')} chars")
    record("look returns elements", isinstance(snap.get("elements"), list),
           f"{len(snap.get('elements') or [])} elements")

    shot = snap.get("screenshot")
    if shot:
        dpr = shot.get("devicePixelRatio")
        expected_w = round(shot["viewportCssWidth"] * dpr)
        record("screenshot captured + uploaded", bool(shot.get("uploadUrl")),
               f"{shot['width']}x{shot['height']} dpr={dpr} url={shot.get('uploadUrl')}")
        record("screenshot width == cssWidth * devicePixelRatio",
               abs(shot["width"] - expected_w) <= 1, f"{shot['width']} vs {expected_w}")
    else:
        record("screenshot captured", False, snap.get("screenshot_error") or "missing")

    return {"active": active}


async def full(hub, original_url: str | None) -> None:
    print("\n== Snapshot + interaction (fixture pages) ==", flush=True)
    nav, err = await send(hub, "navigate", {"url": fixture("article.html")}, timeout=90)
    record("navigate to fixture", bool(nav) and "article.html" in nav.get("url", ""),
           err or str(nav and nav.get("url")))

    snap = await look(hub)
    md = snap.get("markdown", "")
    record("markdown has heading", "# Main Heading" in md)
    record("markdown excludes hidden text", "must not appear" not in md)
    els = snap.get("elements", [])
    record("button captured with id selector",
           any(e.get("selector") == "#submit-btn" for e in els))
    record("disabled state captured", any(e.get("state", {}).get("disabled") for e in els))
    record("bounding boxes are proportions in [0,1]",
           all(0 <= e["box"]["x"] <= 1 and 0 <= e["box"]["y"] <= 1 for e in els))

    # --- click / fill against the controlled-form fixture -----------------
    await send(hub, "navigate", {"url": fixture("react-form.html")}, timeout=90)
    snap = await look(hub, screenshot=False)
    els = snap.get("elements", [])

    email = next((e for e in els if e.get("role") == "input:text"), None)
    record("text input found in snapshot", email is not None,
           f"selector={email.get('selector') if email else None}")
    record("selector avoids hashed class",
           bool(email) and "css-" not in (email.get("selector") or ""),
           email.get("selector") if email else "")

    _, err = await send(hub, "fill", {"ref": email["ref"], "value": "live@example.com"})
    record("fill by ref", err is None, err or "")

    # The real proof is framework state, not the DOM value — read it back
    # through the page's #state-mirror rather than execute_js, so this doesn't
    # depend on the injection feature it isn't testing.
    async def mirror() -> str:
        s = await look(hub, screenshot=False)
        for line in (s.get("markdown") or "").splitlines():
            if line.startswith("state:"):
                return line
        return ""

    record("React value-tracker defeated (component state updated)",
           "live@example.com" in await mirror(), (await mirror())[:200])

    # A ref from before mirror()'s re-snapshot must fail loudly rather than
    # silently resolving to whatever element now occupies that slot.
    stale_btn = next((e for e in els if e.get("role") == "button"), None)
    _, err = await send(hub, "click", {"ref": stale_btn["ref"]})
    record("stale ref rejected after re-snapshot", bool(err) and "unknown_ref" in err,
           (err or "no error raised")[:160])

    fresh = await look(hub, screenshot=False)
    btn = next((e for e in fresh.get("elements", []) if e.get("role") == "button"), None)
    clicked, err = await send(hub, "click", {"ref": btn["ref"]})
    record("click by fresh ref", err is None, err or str(clicked)[:160])
    record("click actually fired the page handler",
           '"submitted":true' in (await mirror()).replace(" ", ""))

    # --- scroll + stale-viewport ------------------------------------------
    print("\n== Scroll + stale-viewport ==", flush=True)
    await send(hub, "navigate", {"url": fixture("shadow-and-scroll.html")}, timeout=90)
    snap = await look(hub, screenshot=False)
    gen = snap.get("generation")
    els = snap.get("elements", [])

    shadow = next((e for e in els if e.get("name") == "Shadow Button"), None)
    record("open shadow-root element captured", shadow is not None)
    record("shadow element has no document selector",
           bool(shadow) and shadow.get("selector") is None)

    _, err = await send(hub, "click", {"position": {"x": 0.5, "y": 0.5}, "generation": gen})
    record("position targeting works pre-scroll", err is None, err or "")

    scrolled, err = await send(hub, "scroll", {"to": "bottom"})
    record("scroll bumps generation", bool(scrolled) and scrolled.get("generation", 0) > gen,
           err or f"{gen} -> {scrolled and scrolled.get('generation')}")

    _, err = await send(hub, "click", {"position": {"x": 0.5, "y": 0.5}, "generation": gen})
    record("stale position rejected after scroll", bool(err) and "stale_viewport" in err,
           (err or "no error raised")[:160])

    _, err = await send(hub, "click", {"selector": "#bottom-btn"})
    record("selector targeting immune to scroll drift", err is None, err or "")

    # --- execute_js --------------------------------------------------------
    print("\n== execute_js ==", flush=True)
    r, err = await send(hub, "execute_js", {"code": "return 1 + 1;"}, timeout=40)
    record("execute_js basic", bool(r) and r.get("result") == 2, err or str(r)[:160])
    record("execute_js ran in MAIN world", bool(r) and r.get("world") == "MAIN",
           str(r and r.get("world")))

    r, err = await send(hub, "execute_js",
                        {"code": "window.__liveProbe = 'set'; return window.__liveProbe;"},
                        timeout=40)
    record("execute_js reaches page globals (MAIN world)",
           bool(r) and r.get("result") == "set", err or str(r)[:160])

    r, err = await send(hub, "execute_js",
                        {"code": "return document.querySelector('#bottom-btn');"}, timeout=40)
    el = (r or {}).get("result") or {}
    record("DOM node serialised with usable selector",
           el.get("__element") is True and el.get("selector") is not None, str(el)[:160])

    _, err = await send(hub, "execute_js", {"code": "throw new Error('deliberate');"},
                        timeout=40)
    record("execute_js error captured", bool(err) and "deliberate" in err, (err or "")[:120])

    # Strict-CSP site — resolved 2026-08-28: MAIN-world injection is allowed.
    print("\n-- strict-CSP site (github.com) --", flush=True)
    nav, err = await send(hub, "navigate", {"url": "https://github.com"}, timeout=90)
    if err:
        record("navigate to github.com", False, err)
    else:
        record("navigate to github.com", True, nav.get("url", ""))
        r, err = await send(hub, "execute_js", {"code": "return document.title;"}, timeout=40)
        record("execute_js on strict-CSP site", err is None,
               err or f"world={r.get('world')} result={r.get('result')!r}")
        if r:
            record("MAIN-world injection allowed under strict CSP",
                   r.get("world") == "MAIN", f"world used = {r.get('world')}")

    if original_url and original_url.startswith(("http://", "https://")):
        _, err = await send(hub, "navigate", {"url": original_url}, timeout=90)
        record("original tab URL restored", err is None, original_url)


async def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "recon"

    app = build_app()
    hub = app.state.browser_hub
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765,
                                           log_level="error"))
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
            await full(hub, (info.get("active") or {}).get("url"))
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
