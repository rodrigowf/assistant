"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from manager.auth import AuthManager
from manager.config import ManagerConfig
from manager.store import SessionStore

from .connections import ConnectionManager
from .indexer import HistoryIndexer, MemoryWatcher
from .pool import SessionPool
from .routes import agents, auth, chat, config, debug, mcp, orchestrator, sessions, skills, voice

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure CLAUDE_CONFIG_DIR is always set, even if not launched via run.sh.
    project_root = Path(__file__).resolve().parent.parent
    os.environ.setdefault("CLAUDE_CONFIG_DIR", str(project_root / ".claude_config"))

    config = ManagerConfig.load()
    app.state.config = config
    app.state.store = SessionStore(config.project_dir)

    # Detect headless mode: no DISPLAY, or explicit HEADLESS=1 env var
    headless = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes") or (
        not os.environ.get("DISPLAY") and os.name != "nt"
    )
    app.state.auth = AuthManager(headless=headless)

    app.state.connections = ConnectionManager()
    app.state.pool = SessionPool()
    # Background orphan reaper — last-line defense against leaked
    # bundled-claude subprocesses (per-session SIGKILL inside
    # SessionManager is the primary defense).  Cheap when nothing is
    # leaked: a few os.kill(0) liveness checks every 30s.
    await app.state.pool.start_orphan_reaper()

    project_path = Path(config.project_dir)

    memory_watcher = MemoryWatcher(project_path)
    memory_task = asyncio.create_task(memory_watcher.run())
    app.state.memory_watcher = memory_watcher

    history_indexer = HistoryIndexer(project_path, interval_seconds=120)
    history_task = asyncio.create_task(history_indexer.run())
    app.state.history_indexer = history_indexer

    # Pre-warm the SessionStore cache off the event loop so the first
    # /api/sessions request doesn't pay the cold-cache cost (which can
    # be 30–60s on slow storage like the Jetson's SD card).
    async def _prewarm_sessions() -> None:
        try:
            count = await asyncio.to_thread(lambda: len(app.state.store.list_sessions()))
            logger.info("SessionStore cache pre-warmed (%d sessions)", count)
        except Exception:
            logger.exception("SessionStore pre-warm failed")

    prewarm_task = asyncio.create_task(_prewarm_sessions())
    app.state.prewarm_task = prewarm_task

    # Pre-warm the search server so the embedding model is already loaded
    # when the first search_memory/search_history call arrives (~100s on Jetson).
    async def _prewarm_search_server() -> None:
        try:
            from orchestrator.tools.search import _ensure_server
            proc = await _ensure_server()
            if proc is not None:
                logger.info("Search server pre-warmed (PID %d)", proc.pid)
            else:
                logger.warning("Search server pre-warm failed (will retry on first query)")
        except Exception:
            logger.exception("Search server pre-warm failed")

    search_prewarm_task = asyncio.create_task(_prewarm_search_server())
    app.state.search_prewarm_task = search_prewarm_task

    try:
        yield
    finally:
        # Stop the reaper before close_all so it doesn't race against the
        # final shutdown drain (kill_claude_subprocess on a pid that
        # close_all is also handling would just be a redundant SIGTERM).
        try:
            await app.state.pool.stop_orphan_reaper()
        except Exception:
            logger.exception("Error stopping orphan reaper on shutdown")

        # Drain the session pool first so remote SSH + claude children get
        # clean SIGTERMs instead of being orphaned by the backend exiting.
        try:
            await app.state.pool.close_all()
        except Exception:
            logger.exception("Error draining session pool on shutdown")

        # Shut down the warm search server subprocess
        try:
            from orchestrator.tools.search import shutdown_server
            await shutdown_server()
        except Exception:
            logger.exception("Error shutting down search server")

        memory_watcher.stop()
        history_indexer.stop()
        memory_task.cancel()
        history_task.cancel()
        prewarm_task.cancel()
        search_prewarm_task.cancel()
        for task in [memory_task, history_task, prewarm_task, search_prewarm_task]:
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    app = FastAPI(title="Assistant API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Allow all origins for Android app and local dev
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(auth.router)
    app.include_router(orchestrator.router)
    app.include_router(voice.router)
    app.include_router(mcp.router)
    app.include_router(config.router)
    app.include_router(skills.router)
    app.include_router(agents.router)
    app.include_router(debug.router)

    # Serve the compat frontend (React 18, for older devices) at /compat/
    compat_dist = Path(__file__).resolve().parent.parent / "frontend-compat" / "dist"
    if compat_dist.exists():
        app.mount("/compat/assets", StaticFiles(directory=compat_dist / "assets"), name="compat-assets")

        @app.get("/compat")
        @app.get("/compat/")
        async def serve_compat_index():
            return FileResponse(compat_dist / "index.html")

        @app.get("/compat/{full_path:path}")
        async def serve_compat_spa(full_path: str):
            file_path = compat_dist / full_path
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)
            return FileResponse(compat_dist / "index.html")

    # Public files directory (context/public/ — synced across machines, served at URL root).
    # Anything placed under context/public/ is reachable at the matching URL path
    # (e.g. context/public/photo-server/file.py → /photo-server/file.py).
    project_root = Path(__file__).resolve().parent.parent
    context_public = project_root / "context" / "public"
    context_public_resolved = context_public.resolve() if context_public.exists() else None

    # Serve the production frontend build if it exists
    frontend_dist = project_root / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

        @app.get("/")
        async def serve_index():
            return FileResponse(frontend_dist / "index.html")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # 1) Check context/public/ first — runtime-served public files
            #    (visualizations, photo-server, downloads, etc.) without rebuild.
            if context_public_resolved is not None and full_path:
                candidate = (context_public / full_path).resolve()
                # Path traversal guard: candidate must stay under context/public/.
                if (
                    candidate.is_relative_to(context_public_resolved)
                    and candidate.is_file()
                ):
                    return FileResponse(candidate)

            # 2) Then check the built frontend dist for static assets.
            file_path = frontend_dist / full_path
            if file_path.exists() and file_path.is_file():
                return FileResponse(file_path)

            # 3) SPA fallback — serve index.html for client-side routing.
            return FileResponse(frontend_dist / "index.html")

    return app
