"""Tests for the OrchestratorSession voice lifecycle state machine.

Covers:
- The state machine itself: valid transitions, invalid transitions raise,
  IDLE → IDLE is a fast no-op, ENDED is terminal.
- ``end_voice()`` runs the full canonical teardown sequence: provider's
  ``graceful_shutdown_frames`` are pushed through the relay, the relay
  is stopped, the audio recorder is released, and the voice_ending /
  voice_ended broadcasts fire in order.
- Idempotency: a second ``end_voice()`` observes ENDED and returns.
- Concurrent callers piggy-back on the in-flight teardown via
  ``_voice_ended`` instead of re-running.
- Pool: ``await_orchestrator_stop`` blocks while ENDING and returns
  True once the slot clears; returns False on timeout.
- The ``end_voice_session`` agent tool awaits the canonical path
  (no more 1.5s sleep, no more fire-and-forget task).

The provider, relay, and pool are mocked so the tests are deterministic
and run without DashScope/Gemini/OpenAI credentials.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.session import (
    OrchestratorSession,
    VoiceLifecycle,
    _VALID_VOICE_TRANSITIONS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    voice: bool = True,
    pool: MagicMock | None = None,
) -> OrchestratorSession:
    """Build a minimal OrchestratorSession for lifecycle tests.

    Bypasses ``OrchestratorConfig.load`` (which touches the filesystem)
    by constructing a bare config-shaped MagicMock; the lifecycle code
    only reads attributes from ``_context``, never from ``_config``.
    """
    config = MagicMock()
    config.summarizer_model = None
    context = {"pool": pool or MagicMock(), "store": MagicMock()}
    session = OrchestratorSession(
        config=config,
        context=context,
        voice=voice,
        local_id="test-local",
    )
    return session


def _stub_relay(send_shutdown_frames_delay: float = 0.0):
    """Build an AsyncMock-shaped relay double that ``end_voice`` will call."""
    relay = MagicMock()
    sent: list[list[dict]] = []

    async def _send(frames):
        sent.append(frames)
        if send_shutdown_frames_delay > 0:
            await asyncio.sleep(send_shutdown_frames_delay)

    relay.send_shutdown_frames = AsyncMock(side_effect=_send)
    relay.stop = AsyncMock()
    # Expose what was sent for assertions.
    relay._sent_shutdown_frames = sent
    return relay


def _stub_provider(frames: list[dict] | None = None):
    provider = MagicMock()
    provider.provider_name = "fake"
    provider.graceful_shutdown_frames = MagicMock(return_value=frames or [])
    return provider


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestStateMachine:
    def test_initial_state_is_idle(self):
        s = _make_session()
        assert s.voice_state is VoiceLifecycle.IDLE

    def test_valid_transitions_map_is_consistent(self):
        # Each terminal-ish state should be reachable from IDLE through
        # at most three hops — sanity check the graph doesn't accidentally
        # acquire dead branches.
        reachable = {VoiceLifecycle.IDLE}
        for _ in range(4):
            for state in list(reachable):
                reachable |= _VALID_VOICE_TRANSITIONS[state]
        assert reachable == set(VoiceLifecycle)

    def test_ended_can_rearm_to_idle(self):
        """ENDED → IDLE is the re-arm transition driven by
        :meth:`OrchestratorSession.restart_voice`. The OrchestratorSession
        is the tab and outlives its voice connection; ending voice doesn't
        forbid arming a fresh voice connection later on the same session.
        """
        assert _VALID_VOICE_TRANSITIONS[VoiceLifecycle.ENDED] == {
            VoiceLifecycle.IDLE,
        }

    @pytest.mark.asyncio
    async def test_self_transition_is_noop(self):
        s = _make_session()
        # IDLE → IDLE inside the helper should not raise.
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.IDLE)
        assert s.voice_state is VoiceLifecycle.IDLE

    @pytest.mark.asyncio
    async def test_invalid_transition_raises(self):
        s = _make_session()
        # IDLE → ACTIVE is not allowed (must go through STARTING).
        with pytest.raises(RuntimeError, match="Invalid voice transition"):
            async with s._voice_lock:
                s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)


# ---------------------------------------------------------------------------
# end_voice — canonical teardown
# ---------------------------------------------------------------------------


class TestEndVoiceCanonical:
    @pytest.mark.asyncio
    async def test_end_voice_on_idle_session_is_noop(self):
        """No broadcasts, no provider calls — text mode's stop lands here."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=False, pool=pool)
        # voice=False keeps state IDLE; end_voice should bail immediately.
        await s.end_voice("shutdown")
        assert s.voice_state is VoiceLifecycle.IDLE
        pool.broadcast_orchestrator.assert_not_called()

    @pytest.mark.asyncio
    async def test_end_voice_active_session_runs_full_sequence(self):
        """Provider's shutdown frames sent → relay stopped → broadcasts fire."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)

        provider = _stub_provider([{"type": "input_audio_buffer.commit"}])
        relay = _stub_relay()
        recorder = MagicMock()
        s._voice_provider = provider
        s._voice_relay = relay
        s._audio_recorder = recorder

        # Set state to ACTIVE manually (bypassing start_voice_relay).
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        await s.end_voice("user_stop")

        # State landed on ENDED.
        assert s.voice_state is VoiceLifecycle.ENDED
        assert s._voice_ended.is_set()
        assert s._voice_end_reason == "user_stop"

        # Provider's shutdown frames were forwarded to the relay.
        provider.graceful_shutdown_frames.assert_called_once_with()
        relay.send_shutdown_frames.assert_awaited_once()
        assert relay._sent_shutdown_frames == [
            [{"type": "input_audio_buffer.commit"}],
        ]

        # Relay was stopped (low-level teardown).
        relay.stop.assert_awaited_once()

        # Recorder was released, provider handle cleared, voice flag flipped.
        recorder.stop.assert_called_once()
        assert s._voice_provider is None
        assert s._voice_relay is None
        assert s._audio_recorder is None
        # _voice flips back to False so the session is genuinely demoted
        # to text mode and the route handler routes a follow-up
        # voice_start through the "text→voice" restart_voice path
        # instead of "already-voice subscribe" path.
        assert s._voice is False

        # Both broadcasts fired, in order, with the right reason.
        broadcast_calls = pool.broadcast_orchestrator.await_args_list
        # 1) voice_ending, 2) voice_ended, 3) legacy voice_stopped alias
        types_in_order = [call.args[0]["type"] for call in broadcast_calls]
        assert types_in_order == ["voice_ending", "voice_ended", "voice_stopped"]
        assert broadcast_calls[0].args[0]["reason"] == "user_stop"
        assert broadcast_calls[1].args[0]["reason"] == "user_stop"

    @pytest.mark.asyncio
    async def test_end_voice_with_empty_shutdown_frames_skips_send(self):
        """Provider returning ``[]`` (OpenAI) — no send_shutdown_frames call."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)
        provider = _stub_provider(frames=[])
        relay = _stub_relay()
        s._voice_provider = provider
        s._voice_relay = relay

        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        await s.end_voice("agent_end")

        relay.send_shutdown_frames.assert_not_called()
        relay.stop.assert_awaited_once()
        assert s.voice_state is VoiceLifecycle.ENDED

    @pytest.mark.asyncio
    async def test_end_voice_tolerates_shutdown_frame_timeout(self):
        """A hung send_shutdown_frames must not block the close path."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)
        # Frames take longer than the 500ms budget → wait_for raises.
        provider = _stub_provider([{"type": "input_audio_buffer.commit"}])
        relay = _stub_relay(send_shutdown_frames_delay=5.0)
        s._voice_provider = provider
        s._voice_relay = relay

        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        await s.end_voice("client_disconnect")

        # Relay was still stopped despite the shutdown-frame timeout.
        relay.stop.assert_awaited_once()
        assert s.voice_state is VoiceLifecycle.ENDED

    @pytest.mark.asyncio
    async def test_double_end_voice_is_idempotent(self):
        """Second call observes ENDED and returns without rerunning teardown."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)
        provider = _stub_provider(frames=[])
        relay = _stub_relay()
        s._voice_provider = provider
        s._voice_relay = relay
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        await s.end_voice("user_stop")
        # Provider already cleared by the first call — second call is no-op.
        await s.end_voice("user_stop")

        relay.stop.assert_awaited_once()  # not 2x
        # Only one pair of broadcasts (plus legacy voice_stopped).
        types = [c.args[0]["type"] for c in pool.broadcast_orchestrator.await_args_list]
        assert types == ["voice_ending", "voice_ended", "voice_stopped"]

    @pytest.mark.asyncio
    async def test_concurrent_end_voice_callers_piggyback(self):
        """Second concurrent caller awaits ``_voice_ended`` instead of racing."""
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)
        provider = _stub_provider(frames=[])
        # Slow the relay close so we can interleave a second call mid-teardown.
        slow_relay = _stub_relay()
        close_started = asyncio.Event()
        close_can_finish = asyncio.Event()

        async def _slow_stop():
            close_started.set()
            await close_can_finish.wait()

        slow_relay.stop = AsyncMock(side_effect=_slow_stop)
        s._voice_provider = provider
        s._voice_relay = slow_relay
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        t1 = asyncio.create_task(s.end_voice("user_stop"))
        await close_started.wait()
        # Second caller arrives while state == ENDING.
        t2 = asyncio.create_task(s.end_voice("agent_end"))
        # Let t1 finish.
        close_can_finish.set()
        await asyncio.wait_for(asyncio.gather(t1, t2), timeout=2.0)

        # Only ONE close path ran.
        slow_relay.stop.assert_awaited_once()
        # Reason is the FIRST caller's reason — second one piggy-backed.
        assert s._voice_end_reason == "user_stop"


# ---------------------------------------------------------------------------
# restart_voice — re-arm voice on the same OrchestratorSession
# ---------------------------------------------------------------------------


class TestRestartVoice:
    @pytest.mark.asyncio
    async def test_restart_after_end_voice_rearms_state_and_provider(self):
        """After end_voice puts the session in ENDED, restart_voice
        moves it back to IDLE, builds a fresh provider, and flips
        _voice back to True. The session object (and its JSONL,
        agent context, pool slot) is the same — only voice was rebuilt.
        """
        pool = MagicMock()
        pool.broadcast_orchestrator = AsyncMock()
        s = _make_session(voice=True, pool=pool)
        provider = _stub_provider(frames=[])
        relay = _stub_relay()
        s._voice_provider = provider
        s._voice_relay = relay
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        await s.end_voice("user_stop")
        assert s.voice_state is VoiceLifecycle.ENDED
        assert s._voice is False
        assert s._voice_provider is None

        # Mock the voice registry so restart_voice doesn't need real
        # provider SDKs.
        fresh_provider = MagicMock()
        fresh_provider.provider_name = "google"
        with patch(
            "orchestrator.providers.voice_registry.resolve_voice_target",
            return_value=("google", {"id": "gemini-x"}, "Puck", "auto"),
        ), patch(
            "orchestrator.providers.voice_registry.instantiate_provider",
            return_value=fresh_provider,
        ), patch(
            "orchestrator.session.is_recording_enabled",
            return_value=False,
        ):
            await s.restart_voice(
                voice_provider="google",
                voice_model="gemini-x",
                voice_name="Puck",
            )

        # State is back to IDLE — start_voice_relay can now drive
        # IDLE → STARTING → ACTIVE as on a fresh voice_start.
        assert s.voice_state is VoiceLifecycle.IDLE
        # Voice flag restored, fresh provider installed.
        assert s._voice is True
        assert s._voice_provider is fresh_provider
        # _voice_ended Event was reset so the next end_voice can be awaited.
        assert not s._voice_ended.is_set()

    @pytest.mark.asyncio
    async def test_restart_on_idle_session_is_noop_for_state(self):
        """Calling restart_voice on a session that never armed voice
        (state == IDLE, _voice == False) just builds the provider and
        flips _voice. No state churn — start_voice_relay will drive
        IDLE → STARTING → ACTIVE as usual.
        """
        s = _make_session(voice=False)
        assert s.voice_state is VoiceLifecycle.IDLE

        fresh_provider = MagicMock()
        fresh_provider.provider_name = "google"
        with patch(
            "orchestrator.providers.voice_registry.resolve_voice_target",
            return_value=("google", {"id": "gemini-x"}, "Puck", "auto"),
        ), patch(
            "orchestrator.providers.voice_registry.instantiate_provider",
            return_value=fresh_provider,
        ), patch(
            "orchestrator.session.is_recording_enabled",
            return_value=False,
        ):
            await s.restart_voice(
                voice_provider="google",
                voice_model="gemini-x",
                voice_name="Puck",
            )

        assert s.voice_state is VoiceLifecycle.IDLE
        assert s._voice is True
        assert s._voice_provider is fresh_provider

    @pytest.mark.asyncio
    async def test_restart_on_active_session_raises(self):
        """Refuse to restart while voice is live (ACTIVE/STARTING/ENDING)
        — the caller is supposed to await end_voice first. Silently
        corrupting state would be worse than the explicit error.
        """
        s = _make_session(voice=True)
        async with s._voice_lock:
            s._set_voice_state_unlocked(VoiceLifecycle.STARTING)
            s._set_voice_state_unlocked(VoiceLifecycle.ACTIVE)

        with pytest.raises(RuntimeError, match="restart_voice called"):
            await s.restart_voice(voice_provider="google", voice_model="x", voice_name="Puck")


# ---------------------------------------------------------------------------
# Pool: await_orchestrator_stop
# ---------------------------------------------------------------------------


class TestPoolAwaitOrchestratorStop:
    @pytest.mark.asyncio
    async def test_returns_true_when_no_session_stopping(self):
        from api.pool import SessionPool
        pool = SessionPool()
        # No session at all.
        assert await pool.await_orchestrator_stop("anything", timeout=0.1) is True

    @pytest.mark.asyncio
    async def test_returns_true_when_different_local_id_stopping(self):
        from api.pool import SessionPool
        pool = SessionPool()
        pool._stopping_orchestrator = MagicMock()
        pool._stopping_orchestrator_id = "other"
        assert await pool.await_orchestrator_stop("ours", timeout=0.1) is True

    @pytest.mark.asyncio
    async def test_blocks_until_stop_completes_then_returns_true(self):
        """The await releases as soon as the stopping slot clears."""
        from api.pool import SessionPool
        pool = SessionPool()
        # Fake a session being torn down: park a MagicMock with a
        # _voice_ended Event and have a background task set it + clear
        # the slot to simulate stop_orchestrator's finally branch.
        stopping = MagicMock()
        stopping._voice_ended = asyncio.Event()
        pool._stopping_orchestrator = stopping
        pool._stopping_orchestrator_id = "ours"

        async def _finish():
            await asyncio.sleep(0.05)
            stopping._voice_ended.set()
            # Clear the slot like stop_orchestrator's finally does.
            pool._stopping_orchestrator = None
            pool._stopping_orchestrator_id = None

        asyncio.create_task(_finish())
        ok = await pool.await_orchestrator_stop("ours", timeout=1.0)
        assert ok is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        from api.pool import SessionPool
        pool = SessionPool()
        # Park a session that NEVER signals voice_ended.
        stopping = MagicMock()
        stopping._voice_ended = asyncio.Event()
        pool._stopping_orchestrator = stopping
        pool._stopping_orchestrator_id = "ours"
        ok = await pool.await_orchestrator_stop("ours", timeout=0.1)
        assert ok is False


# ---------------------------------------------------------------------------
# Tool: end_voice_session
# ---------------------------------------------------------------------------


class TestEndVoiceSessionTool:
    @pytest.mark.asyncio
    async def test_tool_awaits_end_voice_and_keeps_session_alive(self):
        """The tool ends the voice connection but MUST NOT drop the pool
        slot. The orchestrator session (= the tab) outlives any single
        voice connection; the user can re-arm voice via the wake word
        and resume the same conversation.
        """
        from orchestrator.tools.voice_control import end_voice_session

        session = MagicMock()
        session.end_voice = AsyncMock()
        pool = MagicMock()
        pool.get_orchestrator = MagicMock(return_value=session)
        pool.stop_orchestrator = AsyncMock()

        result = await end_voice_session({"pool": pool})

        # Awaited the canonical path with the right reason.
        session.end_voice.assert_awaited_once_with("agent_end")
        # MUST NOT drop the pool slot — that breaks history continuity
        # because the next wake word would start a fresh JSONL.
        pool.stop_orchestrator.assert_not_awaited()
        assert "ended" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_returns_message_when_no_session(self):
        from orchestrator.tools.voice_control import end_voice_session
        pool = MagicMock()
        pool.get_orchestrator = MagicMock(return_value=None)
        result = await end_voice_session({"pool": pool})
        assert "already ended" in result.lower()

    @pytest.mark.asyncio
    async def test_tool_returns_error_message_when_no_pool(self):
        from orchestrator.tools.voice_control import end_voice_session
        result = await end_voice_session({})
        assert "error" in result.lower()


# ---------------------------------------------------------------------------
# Provider hooks
# ---------------------------------------------------------------------------


class TestProviderShutdownFrames:
    def test_base_default_is_empty(self):
        """OpenAI inherits the base no-op — the wire is the data channel."""
        from orchestrator.providers.voice_base import BaseVoiceProvider
        # Call the unbound method directly to dodge ABC instantiation.
        assert BaseVoiceProvider.graceful_shutdown_frames(MagicMock()) == []

    def test_qwen_sends_commit(self):
        from orchestrator.providers.qwen_voice import QwenVoiceProvider
        # Don't instantiate the full provider; just call the unbound method.
        frames = QwenVoiceProvider.graceful_shutdown_frames(MagicMock())
        assert frames == [{"type": "input_audio_buffer.commit"}]

    def test_gemini_sends_activity_end(self):
        from orchestrator.providers.gemini_voice_base import GeminiVoiceProviderBase
        frames = GeminiVoiceProviderBase.graceful_shutdown_frames(MagicMock())
        assert frames == [{"realtimeInput": {"activityEnd": {}}}]
