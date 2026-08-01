"""Tests for the WebRTC ``response.create`` dispatch-gate.

OpenAI Realtime rejects a second ``response.create`` while the first
response is in flight with ``conversation_already_has_active_response``,
which surfaces in the UI as a "Voice error" bubble and silences the
agent. When the agent fires N tools concurrently, each tool result
produces its own ``conversation.item.create`` + ``response.create``
pair; without coordination, the 2nd+ collide.

The fix is two-layered:

  - Provider tracks ``_response_active`` via ``on_inbound_event`` (set
    on ``response.created``, cleared on terminal ``response.done`` /
    cancelled / failed).
  - Dispatch layer (``api/routes/orchestrator._dispatch_voice_commands``)
    consults ``provider.should_gate_event`` per command. Gated frames
    go into a per-session single-slot queue; the inbound-event mirror
    drains the slot once the provider reports ``gate_cleared``.

These tests pin both layers.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from api.routes.orchestrator import (
    _arm_deferred_drain_watchdog,
    _dispatch_voice_commands,
    _drain_deferred_response_create,
)
from orchestrator.config import OrchestratorConfig
from orchestrator.providers.openai_voice import OpenAIVoiceProvider
from orchestrator.session import OrchestratorSession


def _make_session(tmp_path: Path) -> OrchestratorSession:
    config = OrchestratorConfig(
        project_dir=str(tmp_path),
        memory_path=str(tmp_path / "mem.md"),
    )
    session = OrchestratorSession(config=config, context={}, voice=True)
    session._voice_provider = OpenAIVoiceProvider()
    return session


class _Pool:
    """Minimal SessionPool stand-in capturing broadcast payloads."""

    def __init__(self) -> None:
        self.broadcasts: list[dict] = []

    async def broadcast_orchestrator(self, payload: dict) -> None:
        self.broadcasts.append(payload)


# ---------- Provider-level gating ---------------------------------------


def test_openai_provider_gates_response_create_when_active():
    p = OpenAIVoiceProvider()
    # No response in flight → not gated.
    assert p.should_gate_event({"type": "response.create"}) is False
    # Inbound response.created sets the flag.
    p.on_inbound_event({"type": "response.created"})
    assert p.should_gate_event({"type": "response.create"}) is True
    # Other frame types are never gated.
    assert p.should_gate_event({"type": "conversation.item.create"}) is False
    # Terminal event clears the flag.
    p.on_inbound_event({"type": "response.done"})
    assert p.should_gate_event({"type": "response.create"}) is False


@pytest.mark.parametrize(
    "terminal",
    ["response.done", "response.cancelled", "response.failed"],
)
def test_openai_provider_gate_clears_on_all_terminal_events(terminal):
    p = OpenAIVoiceProvider()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    p.on_inbound_event({"type": terminal})
    assert p.gate_cleared() is True


def test_openai_provider_mark_response_create_sent_flips_optimistically():
    """The dispatch-time hook must flip the gate before the round-trip."""
    p = OpenAIVoiceProvider()
    assert p.gate_cleared() is True
    p.mark_response_create_sent()
    assert p.gate_cleared() is False
    # Eventual response.created from upstream is a no-op (already True).
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    p.on_inbound_event({"type": "response.done"})
    assert p.gate_cleared() is True


# ---------- Barge-in gate-clearing (2026-07-24 wedge) --------------------


@pytest.mark.parametrize(
    "interrupt",
    ["input_audio_buffer.speech_started", "output_audio_buffer.cleared"],
)
def test_openai_provider_gate_clears_on_barge_in(interrupt):
    """A barge-in tears down the response; the gate must not stick True.

    Regression for the wedge where ``speech_started`` cancelled a response
    whose ``response.done`` was never mirrored, leaving ``_response_active``
    stuck True and parking every subsequent tool-result ``response.create``.
    """
    p = OpenAIVoiceProvider()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    # User barges in (or the output buffer is cleared) — gate clears without
    # ever seeing a terminal response.done.
    p.on_inbound_event({"type": interrupt})
    assert p.gate_cleared() is True
    # And a fresh response.create is no longer gated.
    assert p.should_gate_event({"type": "response.create"}) is False


def test_openai_provider_barge_in_noop_when_no_response_active():
    """A speech_started with no response in flight must not corrupt state."""
    p = OpenAIVoiceProvider()
    assert p.gate_cleared() is True
    p.on_inbound_event({"type": "input_audio_buffer.speech_started"})
    assert p.gate_cleared() is True


# ---------- Staleness watchdog -------------------------------------------


def test_openai_provider_gate_force_cleared_when_stale(monkeypatch):
    """A gate active past the staleness window is force-cleared.

    Backstops the barge-in fix: even if BOTH the terminal event and the
    interrupt signal are lost, the gate self-heals instead of wedging the
    session forever.
    """
    import orchestrator.providers.openai_voice as ov

    clock = {"t": 1000.0}
    monkeypatch.setattr(ov.time, "monotonic", lambda: clock["t"])

    p = OpenAIVoiceProvider()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False

    # Just under the threshold — still gated.
    clock["t"] += p._RESPONSE_ACTIVE_STALE_SECONDS - 0.1
    assert p.gate_cleared() is False
    assert p.should_gate_event({"type": "response.create"}) is True

    # Past the threshold — force-cleared on the next check.
    clock["t"] += 0.2
    assert p.gate_cleared() is True
    # And a subsequent should_gate_event sees a clean gate.
    assert p.should_gate_event({"type": "response.create"}) is False


def test_openai_provider_stale_clock_resets_on_reactivation(monkeypatch):
    """Each activation restarts the staleness clock (no premature clear)."""
    import orchestrator.providers.openai_voice as ov

    clock = {"t": 500.0}
    monkeypatch.setattr(ov.time, "monotonic", lambda: clock["t"])

    p = OpenAIVoiceProvider()
    p.on_inbound_event({"type": "response.created"})
    # Age most of the way to stale, then a fresh response.created restarts it.
    clock["t"] += p._RESPONSE_ACTIVE_STALE_SECONDS - 0.1
    p.on_inbound_event({"type": "response.created"})
    # Only 0.2s past the *first* activation — but the clock reset, so still gated.
    clock["t"] += 0.2
    assert p.gate_cleared() is False


# ---------- Dispatch-layer behaviour -------------------------------------


@pytest.mark.asyncio
async def test_dispatch_ships_response_create_when_gate_clear(tmp_path):
    session = _make_session(tmp_path)
    pool = _Pool()
    await _dispatch_voice_commands(
        pool, session,
        [
            {"type": "conversation.item.create", "item": {}},
            {"type": "response.create"},
        ],
    )
    # Both frames went out to the frontend.
    types = [b["command"]["type"] for b in pool.broadcasts]
    assert types == ["conversation.item.create", "response.create"]
    # The provider's gate flipped optimistically — a follow-up
    # dispatch on this same session must now defer.
    assert session.voice_provider.gate_cleared() is False


@pytest.mark.asyncio
async def test_dispatch_defers_response_create_when_gate_active(tmp_path):
    session = _make_session(tmp_path)
    pool = _Pool()
    # Simulate the first tool's response.create already in flight upstream.
    session.voice_provider.on_inbound_event({"type": "response.created"})

    await _dispatch_voice_commands(
        pool, session,
        [
            {"type": "conversation.item.create", "item": {"call_id": "t2"}},
            {"type": "response.create"},
        ],
    )
    # item.create still ships immediately — it doesn't conflict.
    types = [b["command"]["type"] for b in pool.broadcasts]
    assert types == ["conversation.item.create"]
    # response.create parked in the single-slot deferred queue.
    assert session._deferred_response_create == {"type": "response.create"}


@pytest.mark.asyncio
async def test_dispatch_coalesces_multiple_deferred_response_creates(tmp_path):
    """Three parallel tools → three response.creates → exactly one queued."""
    session = _make_session(tmp_path)
    pool = _Pool()
    session.voice_provider.on_inbound_event({"type": "response.created"})

    # Tool 2, 3, 4 all complete while tool 1's response is still in flight.
    for call_id in ("t2", "t3", "t4"):
        await _dispatch_voice_commands(
            pool, session,
            [
                {"type": "conversation.item.create", "item": {"call_id": call_id}},
                {"type": "response.create"},
            ],
        )
    # All three item.creates shipped.
    item_creates = [
        b for b in pool.broadcasts
        if b["command"]["type"] == "conversation.item.create"
    ]
    assert len(item_creates) == 3
    # Zero response.creates shipped during the active window.
    response_creates = [
        b for b in pool.broadcasts if b["command"]["type"] == "response.create"
    ]
    assert response_creates == []
    # Single-slot queue holds exactly one (latest writes win, but all
    # response.creates here are identical no-arg frames).
    assert session._deferred_response_create == {"type": "response.create"}


@pytest.mark.asyncio
async def test_drain_ships_deferred_response_create_after_terminal(tmp_path):
    session = _make_session(tmp_path)
    pool = _Pool()
    # Park a deferred response.create.
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session,
        [{"type": "response.create"}],
    )
    assert session._deferred_response_create is not None

    # Simulate the upstream completing the in-flight response — the
    # provider clears its flag, then the drain should ship the parked frame.
    session.voice_provider.on_inbound_event({"type": "response.done"})
    await _drain_deferred_response_create(pool, session)

    response_creates = [
        b for b in pool.broadcasts if b["command"]["type"] == "response.create"
    ]
    assert len(response_creates) == 1
    # Slot drained.
    assert session._deferred_response_create is None
    # Drain also flipped the gate optimistically so a concurrent
    # dispatch can't ship a second response.create over the upstream.
    assert session.voice_provider.gate_cleared() is False


@pytest.mark.asyncio
async def test_drain_is_noop_when_no_deferred_frame(tmp_path):
    session = _make_session(tmp_path)
    pool = _Pool()
    # Gate clear, slot empty — drain must not invent a response.create.
    await _drain_deferred_response_create(pool, session)
    assert pool.broadcasts == []


@pytest.mark.asyncio
async def test_drain_is_noop_when_gate_still_active(tmp_path):
    session = _make_session(tmp_path)
    pool = _Pool()
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session,
        [{"type": "response.create"}],
    )
    assert session._deferred_response_create is not None
    # No terminal event yet — drain must keep the slot held.
    await _drain_deferred_response_create(pool, session)
    assert session._deferred_response_create is not None
    response_creates = [
        b for b in pool.broadcasts if b["command"]["type"] == "response.create"
    ]
    assert response_creates == []


# ---------- Qwen gate parity (2026-08-01) --------------------------------
#
# Qwen gates the same frame as OpenAI but originally cleared only on
# ``response.done`` — no cancelled/failed, no barge-in, no staleness escape,
# no optimistic flip. That made it strictly weaker against the same wedge
# fixed for OpenAI in 9ef6dc1. These pin the ported behaviour.


def _qwen():
    from orchestrator.providers.qwen_voice import QwenVoiceProvider

    return QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")


def test_qwen_gates_response_create_when_active():
    p = _qwen()
    assert p.should_gate_event({"type": "response.create"}) is False
    p.on_inbound_event({"type": "response.created"})
    assert p.should_gate_event({"type": "response.create"}) is True
    # Non-response.create frames are never gated.
    assert p.should_gate_event({"type": "conversation.item.create"}) is False


@pytest.mark.parametrize(
    "terminal",
    ["response.done", "response.cancelled", "response.failed"],
)
def test_qwen_gate_clears_on_all_terminal_events(terminal):
    """Previously only ``response.done`` cleared it — the other two wedged."""
    p = _qwen()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    p.on_inbound_event({"type": terminal})
    assert p.gate_cleared() is True


@pytest.mark.parametrize(
    "interrupt",
    ["input_audio_buffer.speech_started", "output_audio_buffer.cleared"],
)
def test_qwen_gate_clears_on_barge_in(interrupt):
    """DEFAULT_VAD sets interrupt_response=True, so a barge-in really does
    tear the response down server-side — the gate must follow."""
    p = _qwen()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    p.on_inbound_event({"type": interrupt})
    assert p.gate_cleared() is True
    assert p.should_gate_event({"type": "response.create"}) is False


def test_qwen_barge_in_noop_when_no_response_active():
    p = _qwen()
    assert p.gate_cleared() is True
    p.on_inbound_event({"type": "input_audio_buffer.speech_started"})
    assert p.gate_cleared() is True


def test_qwen_gate_force_cleared_when_stale():
    """With every event lost, the gate still self-heals."""
    p = _qwen()
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False

    # Backdate the activation stamp rather than patching time.monotonic
    # module-wide (which would also freeze the event loop's clock).
    p._response_active_since -= p._RESPONSE_ACTIVE_STALE_SECONDS - 0.1
    assert p.gate_cleared() is False

    p._response_active_since -= 0.2
    assert p.gate_cleared() is True
    assert p.should_gate_event({"type": "response.create"}) is False


def test_qwen_mark_response_create_sent_flips_optimistically():
    """Was a base-class no-op on Qwen, so parallel dispatches could collide."""
    p = _qwen()
    assert p.gate_cleared() is True
    p.mark_response_create_sent()
    assert p.gate_cleared() is False
    # The eventual upstream response.created is a no-op (already True).
    p.on_inbound_event({"type": "response.created"})
    assert p.gate_cleared() is False
    p.on_inbound_event({"type": "response.done"})
    assert p.gate_cleared() is True


@pytest.fixture
def fast_watchdog(monkeypatch):
    """Shrink the watchdog's poll/bound so tests don't wait real seconds.

    Only the timing constants change — the drain logic under test is
    untouched.
    """
    import api.routes.orchestrator as orch

    monkeypatch.setattr(orch, "_DEFERRED_DRAIN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(orch, "_DEFERRED_DRAIN_MAX_SECONDS", 0.5)


# ---------- Tool-call path drain (2026-07-31 wedge) -----------------------
#
# The dispatch/drain layers above were both correct in isolation; the wedge
# was that the ONLY drain trigger sat at the tail of ``_handle_voice_event``,
# while tool results dispatch from a separate background task that never
# reaches it. Shipping the parked frame is what causes the next inbound
# event, so "wait for an inbound event to drain" was a circular wait.


@pytest.mark.asyncio
async def test_tool_call_path_drains_parked_frame_without_inbound_event(tmp_path, fast_watchdog):
    """A tool result must not leave ``response.create`` parked forever.

    Regression for the wedge: 14 ``voice_command_deferred`` / 0 drains in
    production. Fails against the pre-fix code, where the tool-call task
    dispatched and returned without ever draining.
    """
    session = _make_session(tmp_path)
    pool = _Pool()

    # A response is in flight, so the tool result's response.create parks.
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session, [{"type": "response.create"}],
    )
    assert session._deferred_response_create is not None

    # The in-flight response terminates. Crucially we do NOT route this
    # through _handle_voice_event — mirroring the real path, where the gate
    # clears but no drain trigger fires.
    session.voice_provider.on_inbound_event({"type": "response.done"})

    # Arm the watchdog as the tool-call path now does.
    _arm_deferred_drain_watchdog(pool, session)
    task = session._deferred_drain_task
    assert task is not None
    await asyncio.wait_for(task, timeout=10.0)

    # The parked frame shipped with no inbound event to prompt it.
    response_creates = [
        b for b in pool.broadcasts if b["command"]["type"] == "response.create"
    ]
    assert len(response_creates) == 1
    assert session._deferred_response_create is None


@pytest.mark.asyncio
async def test_watchdog_drains_via_provider_staleness_when_terminal_lost(
    tmp_path, fast_watchdog,
):
    """Even with the terminal event lost entirely, the frame eventually ships.

    The provider's staleness force-clear is evaluated only when something
    calls ``gate_cleared``; with no inbound events the watchdog is that caller.
    """
    session = _make_session(tmp_path)
    pool = _Pool()
    provider = session.voice_provider
    provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session, [{"type": "response.create"}],
    )
    assert session._deferred_response_create is not None

    # Age the gate past the staleness window by backdating its activation
    # stamp. Patching ``time.monotonic`` module-wide would freeze the event
    # loop's own clock and hang ``asyncio.sleep`` inside the watchdog.
    provider._response_active_since -= (
        provider._RESPONSE_ACTIVE_STALE_SECONDS + 1.0
    )

    # No terminal event ever arrives — only the watchdog can save this.
    _arm_deferred_drain_watchdog(pool, session)
    task = session._deferred_drain_task
    assert task is not None
    await asyncio.wait_for(task, timeout=10.0)

    response_creates = [
        b for b in pool.broadcasts if b["command"]["type"] == "response.create"
    ]
    assert len(response_creates) == 1
    assert session._deferred_response_create is None


@pytest.mark.asyncio
async def test_watchdog_is_single_flight(tmp_path, fast_watchdog):
    """N parallel tool results collapse onto one watchdog, matching the slot."""
    session = _make_session(tmp_path)
    pool = _Pool()
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session, [{"type": "response.create"}],
    )

    _arm_deferred_drain_watchdog(pool, session)
    first = session._deferred_drain_task
    _arm_deferred_drain_watchdog(pool, session)
    _arm_deferred_drain_watchdog(pool, session)
    assert session._deferred_drain_task is first

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_watchdog_not_armed_when_nothing_parked(tmp_path):
    """No parked frame → no timer. Keeps the happy path free of stray tasks."""
    session = _make_session(tmp_path)
    pool = _Pool()
    _arm_deferred_drain_watchdog(pool, session)
    assert session._deferred_drain_task is None


@pytest.mark.asyncio
async def test_end_voice_cancels_drain_watchdog(tmp_path, fast_watchdog):
    """Teardown must not leave a timer polling a torn-down provider."""
    session = _make_session(tmp_path)
    pool = _Pool()
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session, [{"type": "response.create"}],
    )
    _arm_deferred_drain_watchdog(pool, session)
    task = session._deferred_drain_task
    assert task is not None

    await session.end_voice("test")
    assert session._deferred_drain_task is None
    # ``cancel()`` only *requests* cancellation — the task stays in the
    # "cancelling" state until the loop gets a chance to deliver it, so
    # await it here rather than asserting on done() immediately.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_end_voice_clears_deferred_slot(tmp_path):
    """A re-armed voice session must not inherit a stale parked frame."""
    from orchestrator.session import VoiceLifecycle

    session = _make_session(tmp_path)
    pool = _Pool()
    session.voice_provider.on_inbound_event({"type": "response.created"})
    await _dispatch_voice_commands(
        pool, session,
        [{"type": "response.create"}],
    )
    assert session._deferred_response_create is not None

    # Push the session past IDLE so end_voice runs its teardown body
    # instead of short-circuiting on the fast-path.
    session._voice_state = VoiceLifecycle.ACTIVE
    # Stub the broadcast hook invoked from end_voice.
    session._broadcast_voice_lifecycle = AsyncMock()
    await session.end_voice("test")
    assert session._deferred_response_create is None
