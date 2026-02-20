"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from manager.auth import AuthManager
from manager.config import ManagerConfig
from manager.store import SessionStore

from .connections import ConnectionManager, OrchestratorConnectionManager
from .indexer import HistoryIndexer, MemoryWatcher
from .pool import SessionPool
from .routes import auth, chat, orchestrator, sessions, voice

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure CLAUDE_CONFIG_DIR is always set, even if not launched via run.sh.
    # Without this, SDK-spawned Claude Code subprocesses write to the project
    # root instead of .claude_config/.
    project_root = Path(__file__).resolve().parent.parent
    os.environ.setdefault("CLAUDE_CONFIG_DIR", str(project_root / ".claude_config"))

    config = ManagerConfig.load()
    app.state.config = config
    app.state.store = SessionStore(config.project_dir)
    app.state.auth = AuthManager()
    app.state.connections = ConnectionManager()
    app.state.orchestrator_connections = OrchestratorConnectionManager()
    app.state.pool = SessionPool()

    project_path = Path(config.project_dir)

    # Start memory watcher (indexes on file changes)
    memory_watcher = MemoryWatcher(project_path)
    memory_task = asyncio.create_task(memory_watcher.run())
    app.state.memory_watcher = memory_watcher

    # Start periodic history indexer (every 2 min if changed)
    history_indexer = HistoryIndexer(project_path, interval_seconds=120)
    history_task = asyncio.create_task(history_indexer.run())
    app.state.history_indexer = history_indexer

    try:
        yield
    finally:
        # Stop both indexers on shutdown
        memory_watcher.stop()
        history_indexer.stop()
        memory_task.cancel()
        history_task.cancel()
        for task in [memory_task, history_task]:
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    app = FastAPI(title="Assistant API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(auth.router)
    app.include_router(orchestrator.router)
    app.include_router(voice.router)

    return app
