"""Ending an orchestrator session never stops the agent turns it delegated (2026-10-10).

``OrchestratorSession.stop()`` runs when the orchestrator tab closes, on
``switch_conversation``, on a voice rebuild and when a voice start drops a dying
session — it used to ``cancel_all()`` the runner, interrupting every agent
session the conversation had started.  The runner and its notification queue
now belong to the conversation (``AgentRuntimes``, keyed by ``jsonl_id``): the
turn keeps running and the conversation's next session receives its result.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

from manager.types import TextComplete, TurnComplete
from orchestrator.config import OrchestratorConfig
from orchestrator.runner import AgentRuntimes
from orchestrator.session import OrchestratorSession


class _Pool:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.interrupts: list[str] = []
        sm = MagicMock()
        sm.sdk_session_id = "sdk-a"
        sm.pending_permission_ids = MagicMock(return_value=[])
        self.sessions = {"agent-1": sm}

    def has(self, sid: str) -> bool:
        return sid in self.sessions

    async def ensure_live(self, sid: str) -> bool:
        return sid in self.sessions

    def get(self, sid: str) -> Any:
        return self.sessions.get(sid)

    async def interrupt(self, sid: str) -> None:
        self.interrupts.append(sid)

    async def send(self, sid: str, message: str, *, source_ws=None):
        await self.release.wait()
        yield TextComplete(text="digest written")
        yield TurnComplete(session_id="sdk-a", cost=0.5, num_turns=3)


def _store() -> MagicMock:
    store = MagicMock()
    store.get_session_info.return_value = None
    return store


def _session(pool: _Pool, runtimes: AgentRuntimes, *, resume: str | None = None, local: str = "tab-1") -> OrchestratorSession:
    context = {"pool": pool, "store": _store(), "agent_runtimes": runtimes}
    return OrchestratorSession(config=OrchestratorConfig(), context=context, session_id=resume, local_id=local)


async def test_stop_leaves_the_delegated_turn_running_and_the_next_session_gets_its_result() -> None:
    pool, runtimes = _Pool(), AgentRuntimes()
    first = _session(pool, runtimes, local="conv-1")
    handle = await first.runner.spawn("agent-1", "write the digest")  # type: ignore[union-attr]

    await first.stop()
    await asyncio.sleep(0.05)
    assert pool.interrupts == []
    assert [h.turn_id for h in first.runner.list_in_flight()] == [handle.turn_id]  # type: ignore[union-attr]

    # The same conversation reopened (here: resumed by its jsonl id from another tab).
    second = _session(pool, runtimes, resume="conv-1", local="tab-2")
    assert second.runner is first.runner
    assert second.notifications is first.notifications

    pool.release.set()
    await asyncio.sleep(0.05)
    [n] = second.notifications.drain()
    assert n.turn_id == handle.turn_id
    assert n.status == "succeeded"


async def test_stopping_an_old_session_keeps_the_new_sessions_wake_callback() -> None:
    pool, runtimes = _Pool(), AgentRuntimes()
    old = _session(pool, runtimes, local="conv-2")
    new = _session(pool, runtimes, local="conv-2")

    async def wake() -> None:
        return None

    old.notifications.set_wake_callback(wake, owner=old)
    new.notifications.set_wake_callback(wake, owner=new)
    await old.stop()
    assert new.notifications._wake_cb is wake


def test_other_conversations_get_their_own_runner() -> None:
    pool, runtimes = _Pool(), AgentRuntimes()
    a = _session(pool, runtimes, local="conv-a")
    b = _session(pool, runtimes, local="conv-b")
    assert a.runner is not b.runner


def test_a_turn_that_did_not_finish_is_flagged_to_the_model() -> None:
    """2026-10-10: on a `timeout` notification the orchestrator answered with its own
    summary and never said the delegated session had been stopped."""
    from orchestrator.runner import Notification
    from orchestrator.session import _render_notifications

    def note(status: str) -> Notification:
        return Notification("n", "turn-123", "agent-1", None, None, status, 0.0, 0, 600.0,
                            None if status == "succeeded" else "no progress for 1800s")

    assert "did NOT finish" in _render_notifications([note("timeout")])
    assert "did NOT finish" not in _render_notifications([note("succeeded")])
