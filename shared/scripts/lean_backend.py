"""Lean test backend: the real app minus the PyTorch-loading background jobs.

For hands-on tests on the laptop (a worktree or the main checkout) without loading PyTorch: the
memory watcher, history indexer and search-server pre-warm are stubbed; everything else (routes,
pool, watchers, the content watcher, the guard) is real. Binds 127.0.0.1 only.

Run from a checkout root (it serves that checkout's apps/web/dist):
    PYTHONPATH=backend .venv/bin/python shared/scripts/lean_backend.py [port, default 8766]
Don't use 8766 while the browser-extension daemon runs (it uses that port); 8777 is free.
LEAN_ENV_FILE=<path> points the Accounts env-key manager at a scratch copy of context/.env, so
editing keys in the test never touches the real file. In a worktree, context/memory/archie resolves
to the MAIN checkout's docs/, so /memory/archie/... 404s there (a worktree artifact, not a bug).
"""
import asyncio
import os
import sys
from pathlib import Path

import api.app as app_mod


class _Idle:
    def __init__(self, *a, **k):
        pass

    def notify(self, *a, **k):
        pass

    async def run(self):
        await asyncio.Event().wait()


app_mod.MemoryWatcher = _Idle
app_mod.HistoryIndexer = _Idle

import orchestrator.tools.search as search_mod  # noqa: E402


async def _no_server():
    return None


search_mod._ensure_server = _no_server

if os.environ.get("LEAN_ENV_FILE"):
    import manager.accounts.envfile as envfile  # noqa: E402

    _p = Path(os.environ["LEAN_ENV_FILE"])
    envfile.env_path = lambda: _p

import uvicorn  # noqa: E402

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
uvicorn.run(app_mod.create_app(), host="127.0.0.1", port=port, log_level="info")
