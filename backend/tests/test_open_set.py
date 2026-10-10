"""The server owns the open set and it survives restarts (spec 12 OPEN-1..3, 2026-10-10).

Field bug: sessions closed on the web stayed "Open now" on the phone, and a
device's automatic ``start`` could re-create a closed session. Now the pool
persists what is open (``state/open_sessions.json``), restores it at startup
(restored sessions are listed, and spawn on first use), and an automatic
``start`` carries ``reattach``: it subscribes to an open conversation or gets
``error{session_closed}`` — it never re-creates one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from starlette.websockets import WebSocketState

from api.open_sessions import OpenSessionsStore, OpenSet
from api.pool import SessionPool
from manager.types import SessionStatus


def _sm(local_id: str, sdk_id: str | None) -> MagicMock:
    sm = MagicMock()
    sm.local_id = local_id
    sm.sdk_session_id = sdk_id
    sm.status = SessionStatus.IDLE
    sm.cost, sm.turns, sm.provider_name = 0.0, 0, "claude"
    sm.subprocess_pid = None
    sm.start = AsyncMock()
    sm.stop = AsyncMock()
    return sm


def _watcher() -> MagicMock:
    ws = MagicMock()
    ws.client_state = WebSocketState.CONNECTED
    ws.send_bytes = AsyncMock()
    return ws


def _frames(ws: MagicMock) -> list[dict]:
    return [orjson.loads(c.args[0]) for c in ws.send_bytes.call_args_list]


@pytest.fixture
def store(tmp_path: Path) -> OpenSessionsStore:
    return OpenSessionsStore(tmp_path / "open_sessions.json")


def _saved(store: OpenSessionsStore) -> OpenSet:
    return OpenSessionsStore(store._path).load()


async def _create(pool: SessionPool, local_id: str, sdk_id: str | None) -> None:
    with patch("api.pool._session_manager_for", return_value=_sm(local_id, sdk_id)):
        await pool.create(MagicMock(ssh_host=None), local_id=local_id, resume_sdk_id=sdk_id)


# --------------------------------------------------------------------------- store


def test_store_round_trip_and_bad_file(store: OpenSessionsStore) -> None:
    s = OpenSet((("a", "sdk-a"), ("b", None)), ("orch", "jsonl-1"))
    store.save(s)
    assert _saved(store) == s
    store._path.write_text("{not json")
    assert OpenSessionsStore(store._path).load() == OpenSet()


def test_store_skips_unchanged_writes(store: OpenSessionsStore) -> None:
    s = OpenSet((("a", "sdk-a"),))
    store.save(s)
    store._path.write_text(json.dumps({"agents": [], "orchestrator": None}))
    store.save(s)  # same set as last saved: no write
    assert _saved(store) == OpenSet()


# --------------------------------------------------------------------------- pool


async def test_create_and_close_persist_the_open_set(store: OpenSessionsStore) -> None:
    pool = SessionPool()
    pool.restore_open_set(store)
    await _create(pool, "a", "sdk-a")
    await _create(pool, "b", "sdk-b")
    assert _saved(store).agents == (("a", "sdk-a"), ("b", "sdk-b"))
    await pool.close("a")
    assert _saved(store).agents == (("b", "sdk-b"),)


async def test_restart_restores_sessions_listed_idle_and_spawned_on_first_use(store: OpenSessionsStore) -> None:
    store.save(OpenSet((("a", "sdk-a"), ("b", None)), ("orch", "jsonl-1")))
    pool = SessionPool()
    pool.restore_open_set(store)

    assert [r["session_id"] for r in pool.list_sessions()] == ["a", "b"]
    assert {r["status"] for r in pool.list_sessions()} == {"idle"}
    assert pool.is_open("a") and not pool.has("a")
    assert pool.restored_sdk_id("a") == "sdk-a"
    assert pool.restored_orchestrator == ("orch", "jsonl-1")
    assert pool.orchestrator_open("orch") and not pool.has_orchestrator()

    built = MagicMock(ssh_host=None)
    with patch("api.session_factory.build_session_config", return_value=(built, None, {})) as factory, \
            patch("api.pool._session_manager_for", return_value=_sm("a", "sdk-a")) as make:
        assert await pool.ensure_live("a")
    factory.assert_called_once_with(resume_sdk_id="sdk-a", mcp_override=None)
    assert make.call_args.kwargs["local_id"] == "a" and make.call_args.kwargs["session_id"] == "sdk-a"
    assert pool.has("a") and pool.restored_sdk_id("a") is None
    # Same order as before the restart: spawning doesn't reshuffle "Open now".
    assert [r["session_id"] for r in pool.list_sessions()] == ["a", "b"]
    assert not await pool.ensure_live("never-open")


async def test_closing_a_restored_session_tells_every_device(store: OpenSessionsStore) -> None:
    store.save(OpenSet((("a", "sdk-a"),), ("orch", "jsonl-1")))
    pool = SessionPool()
    pool.restore_open_set(store)
    ws = _watcher()
    pool.watch(ws)

    await pool.close("a")
    await pool.stop_orchestrator()

    assert _frames(ws) == [
        {"type": "agent_session_closed", "session_id": "a", "is_orchestrator": False},
        {"type": "agent_session_closed", "session_id": "orch", "is_orchestrator": True},
    ]
    assert _saved(store) == OpenSet()
    assert not pool.is_open("a") and not pool.orchestrator_open("orch")


async def test_shutdown_keeps_the_open_set_for_the_next_start(store: OpenSessionsStore) -> None:
    pool = SessionPool()
    pool.restore_open_set(store)
    await _create(pool, "a", "sdk-a")
    orch = MagicMock(jsonl_id="jsonl-1", stop=AsyncMock())
    await pool.set_orchestrator("orch", orch)

    await pool.close_all()

    assert _saved(store) == OpenSet((("a", "sdk-a"),), ("orch", "jsonl-1"))


async def test_in_place_rebuild_keeps_the_conversation_open(store: OpenSessionsStore) -> None:
    pool = SessionPool()
    pool.restore_open_set(store)
    ws = _watcher()
    pool.watch(ws)
    await pool.set_orchestrator("orch", MagicMock(jsonl_id="jsonl-1", stop=AsyncMock()))

    await pool.stop_orchestrator(replacing=True)  # voice drift / ENDING drop

    assert [f["type"] for f in _frames(ws)] == ["agent_session_opened"]  # no close
    assert pool.orchestrator_open("orch") and pool.restored_orchestrator == ("orch", "jsonl-1")
    assert _saved(store).orchestrator == ("orch", "jsonl-1")


async def test_a_fresh_session_is_saved_with_its_sdk_id_once_known(store: OpenSessionsStore) -> None:
    pool = SessionPool()
    pool.restore_open_set(store)
    await _create(pool, "a", None)
    assert _saved(store).agents == (("a", None),)
    pool.get("a").sdk_session_id = "sdk-new"
    pool._save_open_set()  # what pool.send() does on every TurnComplete
    assert _saved(store).agents == (("a", "sdk-new"),)


# --------------------------------------------------------------------------- routes (OPEN-2)


def _client(pool: SessionPool):
    from starlette.testclient import TestClient

    from api.app import create_app

    app = create_app()
    app.state.pool = pool
    app.state.store = MagicMock()
    return TestClient(app)


def _first(ws) -> dict:
    return orjson.loads(ws.receive_bytes())


def test_reattach_to_a_closed_agent_session_never_recreates_it() -> None:
    pool = SessionPool()
    pool.create = AsyncMock()  # must not be called
    with _client(pool).websocket_connect("/api/sessions/chat") as ws:
        ws.send_text(orjson.dumps({"type": "start", "local_id": "gone", "resume_sdk_id": "sdk-x", "reattach": True}).decode())
        assert _first(ws)["error"] == "session_closed"
    pool.create.assert_not_called()


def test_reattach_spawns_a_restored_agent_session_under_its_local_id() -> None:
    pool = SessionPool()
    pool._open_agents["a"] = "sdk-a"  # restored after a restart

    async def create(config, local_id=None, resume_sdk_id=None, fork=False, mcp_servers=None):
        pool._sessions[local_id] = _sm(local_id, resume_sdk_id)
        pool._subscribers[local_id] = set()
        return local_id

    async def started(ws, pool, sm, session_id, resume_from=None):
        await ws.send_bytes(orjson.dumps({"type": "session_started", "session_id": session_id}))

    pool.create = AsyncMock(side_effect=create)
    with patch("api.session_factory.build_session_config", return_value=(MagicMock(), None, {})), \
            patch("api.routes.chat._send_session_started", side_effect=started), \
            _client(pool).websocket_connect("/api/sessions/chat") as ws:
        ws.send_text(orjson.dumps({"type": "start", "local_id": "a", "reattach": True}).decode())
        frames = [_first(ws), _first(ws)]
    assert frames == [{"type": "status", "status": "connecting"}, {"type": "session_started", "session_id": "a"}]
    assert pool.create.call_args.kwargs["local_id"] == "a"
    assert pool.create.call_args.kwargs["resume_sdk_id"] == "sdk-a"


def test_reattach_to_a_closed_orchestrator_conversation_never_recreates_it() -> None:
    pool = SessionPool()
    with _client(pool).websocket_connect("/api/orchestrator/chat") as ws:
        ws.send_text(orjson.dumps({"type": "start", "local_id": "orch-gone", "resume_sdk_id": "j", "reattach": True}).decode())
        assert _first(ws)["error"] == "session_closed"
    assert not pool.has_orchestrator()


def test_a_restored_orchestrator_counts_as_open_for_other_conversations() -> None:
    pool = SessionPool()
    pool._restored_orchestrator = ("orch", "jsonl-1")
    with _client(pool).websocket_connect("/api/orchestrator/chat") as ws:
        ws.send_text(orjson.dumps({"type": "start", "local_id": "another"}).decode())
        assert _first(ws)["error"] == "orchestrator_active"
