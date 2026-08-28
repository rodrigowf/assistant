#!/usr/bin/env python3
"""Drive Rodrigo's real logged-in Chrome from a Claude Code session.

Talks to POST /api/browser/command, which relays to the Chrome extension in
`browser-extension/`. Sessions have Bash but not the extension's WebSocket
(single-client, held by the browser), so this is the command channel.

    context/scripts/run.sh context/scripts/browser_cmd.py look
    context/scripts/run.sh context/scripts/browser_cmd.py click --selector "#submit"
    context/scripts/run.sh context/scripts/browser_cmd.py js "return document.title;"

The loop that matters — the snapshot goes stale the moment the page changes:

    1. `look` first. It returns the markdown, the element refs/selectors, and a
       screenshot. You cannot target anything you haven't looked at.
    2. Target by `--ref` or `--selector` from that output.
    3. `--x/--y` proportional coordinates are a fallback, and are refused once
       the page scrolls.

Endpoint is loopback-only by design; on the laptop that means going through the
`archie-browser-tunnel` service. See browser-extension/PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENDPOINT = "http://127.0.0.1:8765"
TOKEN_ENV_VAR = "BROWSER_CONTROL_TOKEN"

# Truncate the element list by default: a busy page yields hundreds of entries
# and the point of `look` is that its output is readable.
DEFAULT_ELEMENT_LIMIT = 60


def resolve_token() -> str | None:
    """Prefer the environment, else parse context/.env (same as the backend)."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if token and token.strip():
        return token.strip()
    env_path = PROJECT_ROOT / "context" / ".env"
    if not env_path.exists():
        return None
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == TOKEN_ENV_VAR:
            return value.strip().strip('"').strip("'") or None
    return None


def call(command: str, params: dict, timeout: float = 60.0) -> dict:
    token = resolve_token()
    if not token:
        die(f"{TOKEN_ENV_VAR} not found in environment or context/.env")

    endpoint = os.environ.get("BROWSER_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
    payload = json.dumps({"command": command, "params": params}).encode()
    req = urllib.request.Request(
        f"{endpoint}/api/browser/command",
        data=payload,
        headers={"Content-Type": "application/json", "X-Browser-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except Exception:
            pass
        # Translate the transport's status codes into advice, since these are
        # the failures a session will actually hit.
        if e.code == 503 and "not connected" in str(detail):
            die(f"browser not connected: {detail}\n"
                "Is Chrome running with the Archie Browser Control extension, "
                "and is the tunnel up? (systemctl --user status archie-browser-tunnel)")
        if e.code == 403:
            die(f"refused: {detail}")
        if e.code == 401:
            die(f"auth failed: {detail}")
        die(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"cannot reach the backend at {endpoint}: {e.reason}\n"
            "On the laptop this needs the tunnel: "
            "systemctl --user status archie-browser-tunnel")
    return {}


def die(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def target_params(args) -> dict:
    """Build a target from ref / selector / proportional position."""
    if args.ref:
        return {"ref": args.ref}
    if args.selector:
        return {"selector": args.selector}
    if args.x is not None and args.y is not None:
        params = {"position": {"x": args.x, "y": args.y}}
        if args.generation is not None:
            params["generation"] = args.generation
        return params
    die("need a target: --ref or --selector (preferred), or --x/--y. "
        "Run `look` first to get them.")
    return {}


def render_look(result: dict, limit: int, raw: bool) -> None:
    if raw:
        print(json.dumps(result, indent=2))
        return

    print(f"URL:        {result.get('url')}")
    print(f"Title:      {result.get('title')}")
    print(f"Generation: {result.get('generation')}   "
          f"(position targets are only valid for this generation)")
    shot = result.get("screenshot") or {}
    if shot.get("uploadUrl"):
        print(f"Screenshot: {shot['uploadUrl']}  ({shot.get('width')}x{shot.get('height')})")
    elif result.get("screenshot_error"):
        print(f"Screenshot: FAILED — {result['screenshot_error']}")
    if result.get("truncated"):
        print("NOTE:       markdown truncated; re-run with --max-chars for more")
    print()
    print("--- PAGE ---")
    print(result.get("markdown", ""))

    elements = result.get("elements") or []
    print()
    print(f"--- ELEMENTS ({len(elements)}) ---")
    for el in elements[:limit]:
        bits = [f"{el['ref']:>5}", f"{el.get('role',''):<18}"]
        name = (el.get("name") or "")[:45]
        bits.append(f"{name:<45}")
        bits.append(el.get("selector") or "(shadow DOM — use ref)")
        print("  ".join(bits))
    if len(elements) > limit:
        print(f"  … {len(elements) - limit} more (use --limit or --raw)")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="browser_cmd.py",
        description="Control Rodrigo's real logged-in Chrome.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_target(sp):
        sp.add_argument("--ref", help="element ref from `look` (preferred)")
        sp.add_argument("--selector", help="CSS selector from `look`")
        sp.add_argument("--x", type=float, help="proportional x in [0,1] (fallback)")
        sp.add_argument("--y", type=float, help="proportional y in [0,1] (fallback)")
        sp.add_argument("--generation", type=int, help="snapshot generation for --x/--y")

    sp = sub.add_parser("look", help="screenshot + text snapshot (ALWAYS do this first)")
    sp.add_argument("--max-chars", type=int, help="markdown budget (default 40000)")
    sp.add_argument("--no-screenshot", action="store_true")
    sp.add_argument("--limit", type=int, default=DEFAULT_ELEMENT_LIMIT)
    sp.add_argument("--raw", action="store_true", help="full JSON")

    sp = sub.add_parser("navigate", help="open a URL in the active tab")
    sp.add_argument("url")

    sub.add_parser("tabs", help="list open tabs and windows")

    sp = sub.add_parser("switch", help="make another tab active")
    sp.add_argument("tab_id", type=int)

    sp = sub.add_parser("click", help="click an element")
    add_target(sp)

    sp = sub.add_parser("fill", help="fill an input, textarea, select, checkbox")
    add_target(sp)
    sp.add_argument("--value", default="")
    sp.add_argument("--checked", choices=["true", "false"])

    sp = sub.add_parser("scroll", help="scroll the page")
    sp.add_argument("--to", choices=["top", "bottom"])
    sp.add_argument("--pages", type=float, help="viewport heights; negative scrolls up")
    sp.add_argument("--ref")
    sp.add_argument("--selector")

    sp = sub.add_parser("js", help="run JavaScript in the page (async body; use return)")
    sp.add_argument("code")
    sp.add_argument("--world", choices=["MAIN", "USER_SCRIPT"])
    sp.add_argument("--timeout", type=float, default=15.0)

    sub.add_parser("status", help="is the browser connected?")

    args = p.parse_args()

    if args.cmd == "status":
        endpoint = os.environ.get("BROWSER_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")
        try:
            with urllib.request.urlopen(f"{endpoint}/api/browser/status", timeout=10) as r:
                print(json.dumps(json.loads(r.read()), indent=2))
        except Exception as e:
            die(f"cannot reach {endpoint}: {e}")
        return 0

    if args.cmd == "look":
        params = {}
        if args.max_chars:
            params["maxChars"] = args.max_chars
        snap = call("snapshot", params, timeout=90)["result"]
        if not args.no_screenshot:
            try:
                snap["screenshot"] = call("capture_screenshot", {})["result"]
            except SystemExit:
                # A failed screenshot must not discard a good snapshot — the
                # text is the more actionable half.
                snap["screenshot_error"] = "capture failed (see above)"
        render_look(snap, args.limit, args.raw)
        return 0

    if args.cmd == "navigate":
        out = call("navigate", {"url": args.url}, timeout=90)["result"]
    elif args.cmd == "tabs":
        out = call("list_tabs", {})["result"]
    elif args.cmd == "switch":
        out = call("switch_tab", {"tabId": args.tab_id})["result"]
    elif args.cmd == "click":
        out = call("click", target_params(args))["result"]
    elif args.cmd == "fill":
        params = target_params(args)
        params["value"] = args.value
        if args.checked is not None:
            params["checked"] = args.checked == "true"
        out = call("fill", params)["result"]
    elif args.cmd == "scroll":
        params = {}
        for key in ("to", "ref", "selector"):
            if getattr(args, key):
                params[key] = getattr(args, key)
        if args.pages is not None:
            params["pages"] = args.pages
        if not params:
            die("scroll needs one of --to, --pages, --ref, --selector")
        out = call("scroll", params)["result"]
    elif args.cmd == "js":
        params = {"code": args.code, "timeout": int(args.timeout * 1000)}
        if args.world:
            params["world"] = args.world
        out = call("execute_js", params, timeout=args.timeout + 20)["result"]
    else:
        die(f"unknown command {args.cmd}")
        return 1

    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
