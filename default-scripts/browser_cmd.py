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

The daemon that terminates the extension's WebSocket runs on this machine
(`browser_daemon.py`, loopback only) and is started automatically on first use.
Chrome, the daemon and the session are all co-located, so nothing crosses the
network. See browser-extension/PLAN.md.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAEMON = PROJECT_ROOT / "default-scripts" / "browser_daemon.py"
DAEMON_LOG = PROJECT_ROOT / "logs" / "browser-daemon.log"
DEFAULT_ENDPOINT = "http://127.0.0.1:8766"
TOKEN_ENV_VAR = "BROWSER_CONTROL_TOKEN"

# Truncate the element list by default: a busy page yields hundreds of entries
# and the point of `look` is that its output is readable.
DEFAULT_ELEMENT_LIMIT = 60


def env_value(key: str) -> str | None:
    """Prefer the process environment, else parse context/.env."""
    value = os.environ.get(key)
    if value and value.strip():
        return value.strip()
    env_path = PROJECT_ROOT / "context" / ".env"
    if not env_path.exists():
        return None
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        if name.strip() == key:
            return val.strip().strip('"').strip("'") or None
    return None


def resolve_token() -> str | None:
    return env_value(TOKEN_ENV_VAR)


def delegate_target() -> str | None:
    """Return the ssh target when Chrome lives on a different machine.

    The daemon must be co-located with Chrome. Rather than exposing it on the
    LAN or maintaining a tunnel, a caller on another machine re-runs this same
    command over SSH on the machine that has the browser — so the daemon stays
    loopback-only and there is nothing to keep alive.

    Configured in context/.env, which is synced between machines, so the
    "am I the browser host?" test is by hostname rather than by a flag that
    would be true on both:

        BROWSER_HOST_NAME=rodrigo-laptop
        BROWSER_HOST_SSH=rodrigo@192.168.0.28

    Returns None when this machine is the browser host (or nothing is
    configured, i.e. single-machine setups keep working untouched).
    """
    if os.environ.get("BROWSER_FORCE_LOCAL"):
        return None
    host_name = env_value("BROWSER_HOST_NAME")
    if not host_name or host_name == socket.gethostname():
        return None
    target = env_value("BROWSER_HOST_SSH")
    if not target:
        die("BROWSER_HOST_NAME is set but BROWSER_HOST_SSH is missing in context/.env")
    return target


def delegate_over_ssh(target: str, argv: list[str]) -> int:
    """Re-run this command on the browser host, streaming its output back."""
    remote_root = env_value("BROWSER_HOST_PATH") or "~/assistant"
    # Each arg is quoted individually: ssh space-joins its argv, so an unquoted
    # multi-word arg (a JS snippet, a selector) would be re-split remotely.
    inner = " ".join(shlex.quote(a) for a in argv)
    remote = (
        f"cd {remote_root} && BROWSER_FORCE_LOCAL=1 "
        f"context/scripts/run.sh context/scripts/browser_cmd.py {inner}"
    )
    print(f"(running on {target} — the browser host)", file=sys.stderr)
    return subprocess.call([
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, remote,
    ])


def endpoint() -> str:
    return os.environ.get("BROWSER_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def daemon_status(timeout: float = 2.0) -> dict | None:
    """Return the hub status, or None if the daemon isn't answering."""
    try:
        with urllib.request.urlopen(f"{endpoint()}/api/browser/status", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def ensure_daemon(quiet: bool = False, require_browser: bool = True) -> dict:
    """Start the local daemon if it isn't up, then wait for Chrome to attach.

    The daemon only has to live on the machine Chrome is on — which is also
    where Claude Code sessions run — so this is a loopback call, not a network
    round-trip. Starting it here rather than in a separate step means a session
    can't forget to.

    Detached via start_new_session so it outlives the session that started it,
    and idempotent: a second caller finds the port taken and exits cleanly.
    """
    status = daemon_status()
    if status is None:
        if not quiet:
            print("browser daemon not running — starting it…", file=sys.stderr)
        DAEMON_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DAEMON_LOG.open("a") as log:
            subprocess.Popen(
                [sys.executable, str(DAEMON)],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(PROJECT_ROOT),
            )
        for _ in range(40):  # ~20s
            time.sleep(0.5)
            status = daemon_status()
            if status is not None:
                break
        if status is None:
            die(f"daemon failed to start within 20s — see {DAEMON_LOG}")

    if status.get("connected") or not require_browser:
        return status

    # The daemon is up but Chrome hasn't reattached yet. The extension
    # reconnects on its own backoff, so this is a short wait, not a hang.
    if not quiet:
        print("waiting for Chrome extension to attach…", file=sys.stderr)
    for _ in range(30):  # ~30s
        time.sleep(1.0)
        status = daemon_status() or status
        if status.get("connected"):
            return status

    die("browser daemon is running but no Chrome extension attached.\n"
        f"Check the extension popup points at ws://127.0.0.1:{endpoint().rsplit(':',1)[-1]}"
        "/api/browser/ws, and that Chrome is running.")
    return {}


def call(command: str, params: dict, timeout: float = 60.0) -> dict:
    token = resolve_token()
    if not token:
        die(f"{TOKEN_ENV_VAR} not found in environment or context/.env")

    payload = json.dumps({"command": command, "params": params}).encode()
    req = urllib.request.Request(
        f"{endpoint()}/api/browser/command",
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
                "Is Chrome running, with the Archie Browser Control extension "
                "loaded and pointed at this daemon?")
        if e.code == 403:
            die(f"refused: {detail}")
        if e.code == 401:
            die(f"auth failed: {detail}")
        die(f"HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"cannot reach the browser daemon at {endpoint()}: {e.reason}\n"
            f"Start it with: context/scripts/run.sh {DAEMON.relative_to(PROJECT_ROOT)}")
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

    sp = sub.add_parser("navigate", help="open a URL in the active tab (replaces it)")
    sp.add_argument("url")

    sp = sub.add_parser("newtab", help="open a URL in a NEW tab (keeps the current one)")
    sp.add_argument("url")
    sp.add_argument("--background", action="store_true",
                    help="open without focusing it; you must `switch` before acting on it")

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

    sub.add_parser("status", help="is the daemon up and the browser connected?")

    sp = sub.add_parser("daemon", help="manage the local browser daemon")
    sp.add_argument("action", choices=["status", "start", "stop"])

    args = p.parse_args()

    # If Chrome is on another machine, hand the whole invocation to it. Done
    # before any local work so no subcommand needs to know about this.
    target = delegate_target()
    if target:
        return delegate_over_ssh(target, sys.argv[1:])

    if args.cmd == "status":
        st = daemon_status(timeout=5)
        if st is None:
            print(json.dumps({"daemon": "not running", "endpoint": endpoint()}, indent=2))
            return 1
        print(json.dumps({"daemon": "running", "endpoint": endpoint(), **st}, indent=2))
        return 0

    if args.cmd == "daemon":
        if args.action == "status":
            st = daemon_status(timeout=5)
            print(json.dumps(st or {"daemon": "not running"}, indent=2))
            return 0 if st else 1
        if args.action == "start":
            print(json.dumps(ensure_daemon(require_browser=False), indent=2))
            return 0
        if args.action == "stop":
            pid_file = PROJECT_ROOT / "logs" / "browser-daemon.pid"
            if not pid_file.exists():
                die("no pidfile — daemon does not appear to be running")
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"sent SIGTERM to browser daemon (pid {pid})")
            return 0

    # Every command below needs the daemon and an attached browser; doing it
    # here means no subcommand can forget.
    ensure_daemon()

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
    elif args.cmd == "newtab":
        out = call("open_tab", {"url": args.url, "background": args.background},
                   timeout=90)["result"]
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
