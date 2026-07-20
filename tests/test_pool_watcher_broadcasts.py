"""Pool-watcher broadcasts for orchestrator open/close + is_orchestrator field.

Peripheral clients (Android, web) stay in sync with the live pool by listening
to ``agent_session_opened`` / ``agent_session_closed`` frames broadcast to pool
watchers (every orchestrator WS auto-registers as one). Previously the
orchestrator's own pool membership was invisible — only regular agent sessions
broadcast — so a session list on another device stayed stale until a manual
refresh. These tests pin:

1. ``set_orchestrator`` broadcasts ``agent_session_opened`` with
   ``is_orchestrator: True``.
2. ``stop_orchestrator`` broadcasts ``agent_session_closed`` with
   ``is_orchestrator: True`` (and only when one was registered).
3. Regular-session events carry ``is_orchestrator: False`` so a client can
   classify every pool event without a refetch.
"""

from __future__ import annotations

import orjson
import pytest
from unittest.mock import AsyncMock, MagicMock

from starlette.websockets import WebSocketState

from api.pool import SessionPool


def _watcher() -> MagicMock:
    """A fake watcher WS that records the frames sent to it."""
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_bytes = AsyncMock()
    return ws


def _frames(ws: MagicMock) -> list[dict]:
    return [orjson.loads(call.args[0]) for call in ws.send_bytes.call_args_list]


@pytest.mark.asyncio
async def test_set_orchestrator_broadcasts_opened_is_orchestrator_true():
    pool = SessionPool()
    ws = _watcher()
    pool.watch(ws)

    session = MagicMock()
    session.jsonl_id = "jsonl-abc"
    await pool.set_orchestrator("orch-local", session)

    frames = _frames(ws)
    assert len(frames) == 1
    ev = frames[0]
    assert ev["type"] == "agent_session_opened"
    assert ev["session_id"] == "orch-local"
    assert ev["sdk_session_id"] == "jsonl-abc"
    assert ev["is_orchestrator"] is True


@pytest.mark.asyncio
async def test_stop_orchestrator_broadcasts_closed_is_orchestrator_true():
    pool = SessionPool()
    session = AsyncMock()
    session.jsonl_id = "jsonl-abc"
    await pool.set_orchestrator("orch-local", session)

    # Attach the watcher AFTER set so we only capture the close frame.
    ws = _watcher()
    pool.watch(ws)
    await pool.stop_orchestrator()

    frames = _frames(ws)
    assert len(frames) == 1
    ev = frames[0]
    assert ev["type"] == "agent_session_closed"
    assert ev["session_id"] == "orch-local"
    assert ev["is_orchestrator"] is True


@pytest.mark.asyncio
async def test_stop_orchestrator_no_broadcast_when_none_registered():
    pool = SessionPool()
    ws = _watcher()
    pool.watch(ws)

    # No orchestrator was ever set — a redundant stop must not emit a phantom.
    await pool.stop_orchestrator()
    assert _frames(ws) == []


@pytest.mark.asyncio
async def test_set_orchestrator_falls_back_to_session_id_when_no_jsonl_id():
    pool = SessionPool()
    ws = _watcher()
    pool.watch(ws)

    # A session object without jsonl_id — sdk_session_id defaults to the local id.
    session = object()
    await pool.set_orchestrator("orch-local", session)

    ev = _frames(ws)[0]
    assert ev["sdk_session_id"] == "orch-local"
    assert ev["is_orchestrator"] is True
