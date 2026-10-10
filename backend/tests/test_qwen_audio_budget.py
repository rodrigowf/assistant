"""Qwen voice: silence gating, the audio history budget, and upstream errors.

DashScope caps one realtime conversation at 320 "audios" (~630s of
committed input audio, silence included) and fails with ``Too many audios.
The maximum allowed is 320.`` (2026-10-10 field failure after 10.5 min).
The relay now:

- holds silent mic chunks in a short pre-roll instead of committing them
  (``gate_silence_upstream``),
- deletes the oldest user audio items once their total passes the
  provider's ``audio_history_budget_s`` — the same upstream conversation
  stays alive instead of being reopened with a text-only history,
- keeps raw upstream ``error`` events away from clients, which treat a
  voice ``error`` as fatal and tore the session down while the relay was
  reconnecting.
"""

from __future__ import annotations

import base64
import json
import wave
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from orchestrator import voice_vad
from orchestrator.providers.qwen_voice import QwenVoiceProvider
from orchestrator.voice_errors import VoiceErrorCategory
from orchestrator.voice_relay import VoiceRelay

_SPEECH_WAV = Path(__file__).parent / "fixtures" / "voice_speech_24k.wav"


class FakeWS:
    """Captures upstream frames; optionally replays inbound messages."""

    def __init__(self, inbound: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbound = [json.dumps(e) for e in inbound or []]
        self.close_code = None
        self.close_reason = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        pass

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for raw in self._inbound:
            yield raw

    def types(self) -> list[str]:
        return [f.get("type") for f in self.sent]


def _relay(provider: QwenVoiceProvider, on_event=None) -> tuple[VoiceRelay, FakeWS]:
    relay = VoiceRelay(
        provider,
        on_audio_out=AsyncMock(),
        on_event_for_frontend=on_event or AsyncMock(),
        session_id="t-audio-budget",
    )
    ws = FakeWS()
    relay._ws = ws  # type: ignore[assignment]
    relay._handshake_complete.set()
    return relay, ws


def _committed(item_id: str) -> dict:
    return {"type": "input_audio_buffer.committed", "item_id": item_id}


async def _commit_turn(relay: VoiceRelay, item_id: str, secs: float, sr: int = 16000) -> None:
    """One user turn: ``secs`` of audio appended, committed, acked."""
    relay._uncommitted_audio_bytes = int(secs * sr * 2)
    await relay.send_event({"type": "input_audio_buffer.commit"})
    relay._on_audio_committed(_committed(item_id))


# --- silence gating -----------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_is_held_and_only_the_spoken_turn_is_committed(monkeypatch):
    monkeypatch.delenv("QWEN_MANUAL_VAD", raising=False)
    if not _SPEECH_WAV.exists():
        pytest.skip("speech fixture missing")
    with wave.open(str(_SPEECH_WAV)) as w:
        speech = w.readframes(w.getnframes())
    sr = 24000
    lead_silence = b"\x00\x00" * (sr * 3)
    pcm = lead_silence + speech

    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")  # 24 kHz in
    relay, ws = _relay(provider)
    relay._manual_vad = voice_vad.VoiceVAD(
        input_sample_rate=sr, min_silence_duration_ms=600, min_speech_duration_ms=100,
    )

    chunk = 960  # 20 ms
    for i in range(0, len(pcm), chunk):
        await relay.send_audio(base64.b64encode(pcm[i:i + chunk]).decode())

    types = ws.types()
    assert types.count("input_audio_buffer.commit") == 1, types
    assert types.count("response.create") == 1, types
    # The 3 s silent lead (minus the 1 s pre-roll) never went upstream.
    assert relay._audio_held_bytes >= 2 * sr * 2
    appended = sum(
        (len(f["audio"]) * 3) // 4 for f in ws.sent if f["type"] == "input_audio_buffer.append"
    )
    assert appended < len(pcm) - 2 * sr * 2
    # The pre-roll went out first, ahead of the commit.
    assert types[0] == "input_audio_buffer.append"
    # Nothing appended after the turn was committed: trailing silence is held.
    last_commit = max(i for i, t in enumerate(types) if t == "input_audio_buffer.commit")
    assert "input_audio_buffer.append" not in types[last_commit + 1:]
    # The commit's duration is queued for the item it creates.
    assert len(relay._pending_commit_s) == 1
    assert relay._pending_commit_s[0] == pytest.approx(appended / (2 * sr), abs=0.01)


@pytest.mark.asyncio
async def test_no_gating_without_manual_vad(monkeypatch):
    provider = QwenVoiceProvider(model="qwen3.5-omni-plus-realtime")
    relay, ws = _relay(provider)
    assert relay._manual_vad is None
    await relay.send_audio(base64.b64encode(b"\x00\x00" * 480).decode())
    assert ws.types() == ["input_audio_buffer.append"]


# --- audio history budget -----------------------------------------------------


@pytest.mark.asyncio
async def test_oldest_user_audio_items_are_deleted_past_the_budget(monkeypatch):
    monkeypatch.setenv("QWEN_AUDIO_HISTORY_BUDGET_S", "10")
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")  # 16 kHz in
    relay, ws = _relay(provider)

    for i in range(4):
        await _commit_turn(relay, f"a{i}", 4.0)

    deletes = [f["item_id"] for f in ws.sent if f["type"] == "conversation.item.delete"]
    assert deletes == ["a0", "a1"]
    assert [i for i, _ in relay._audio_items] == ["a2", "a3"]
    assert relay._audio_items_s == pytest.approx(8.0)
    assert relay._audio_evicted_items == 2


@pytest.mark.asyncio
async def test_deletes_ship_before_the_turns_response_create(monkeypatch):
    """The model must never prefill a turn over budget: evict at commit time."""
    monkeypatch.setenv("QWEN_AUDIO_HISTORY_BUDGET_S", "10")
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, ws = _relay(provider)
    await _commit_turn(relay, "a0", 6.0)
    ws.sent.clear()

    relay._uncommitted_audio_bytes = 6 * 16000 * 2
    for frame in provider.manual_vad_stop_frames():  # commit + response.create
        await relay.send_event(frame)

    assert ws.types() == [
        "input_audio_buffer.commit", "conversation.item.delete", "response.create",
    ]
    assert ws.sent[1]["item_id"] == "a0"


@pytest.mark.asyncio
async def test_item_creation_before_commit_does_not_shift_durations(monkeypatch):
    """DashScope creates the user item (``in_progress``) while audio streams."""
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, _ = _relay(provider)
    relay._ws = FakeWS(inbound=[  # type: ignore[assignment]
        {"type": "conversation.item.created",
         "item": {"id": "b", "status": "in_progress", "role": "user", "content": [{"type": "input_audio"}]}},
        _committed("b"),
    ])
    relay._uncommitted_audio_bytes = 3 * 16000 * 2
    relay._account_upstream_frame({"type": "input_audio_buffer.commit"})
    await relay._drain()
    assert list(relay._audio_items) == [("b", pytest.approx(3.0))]


@pytest.mark.asyncio
async def test_newest_item_is_never_deleted(monkeypatch):
    monkeypatch.setenv("QWEN_AUDIO_HISTORY_BUDGET_S", "5")
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, ws = _relay(provider)
    await _commit_turn(relay, "only", 30.0)  # one 30 s monologue
    await relay._enforce_audio_budget()
    assert "conversation.item.delete" not in ws.types()
    assert [i for i, _ in relay._audio_items] == ["only"]


@pytest.mark.asyncio
async def test_empty_or_rejected_commits_do_not_shift_item_durations(monkeypatch):
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, _ = _relay(provider)
    relay._account_upstream_frame({"type": "input_audio_buffer.commit"})  # nothing appended
    relay._uncommitted_audio_bytes = 16000  # 0.5 s, then DashScope rejects it
    relay._account_upstream_frame({"type": "input_audio_buffer.commit"})
    relay._on_commit_rejected({"type": "error", "error": {
        "type": "invalid_request_error",
        "message": "Error committing input audio buffer: buffer too small, or have no audio.",
    }})
    relay._uncommitted_audio_bytes = 3 * 16000 * 2
    relay._account_upstream_frame({"type": "input_audio_buffer.commit"})
    relay._on_audio_committed(_committed("a"))
    assert list(relay._audio_items) == [("a", pytest.approx(3.0))]


@pytest.mark.asyncio
async def test_appends_sent_as_control_events_are_counted(monkeypatch):
    """Injected recordings arrive as ``input_audio_buffer.append`` control frames."""
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, _ = _relay(provider)
    audio = base64.b64encode(b"\x00\x00" * 16000 * 2).decode()  # 2 s
    await relay.send_event({"type": "input_audio_buffer.append", "audio": audio})
    await relay.send_event({"type": "input_audio_buffer.commit"})
    assert list(relay._pending_commit_s) == [pytest.approx(2.0, abs=0.01)]


@pytest.mark.asyncio
async def test_reconnect_reset_forgets_the_old_conversation(monkeypatch):
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, _ = _relay(provider)
    await _commit_turn(relay, "old", 1.0)
    relay._reset_audio_tracking()
    assert not relay._audio_items and relay._audio_items_s == 0.0
    assert not relay._pending_commit_s and relay._uncommitted_audio_bytes == 0


def test_qwen_budget_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("QWEN_AUDIO_HISTORY_BUDGET_S", raising=False)
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    assert provider.audio_history_budget_s == 420.0
    assert provider.gate_silence_upstream is True
    assert provider.delete_item_frame("x") == {"type": "conversation.item.delete", "item_id": "x"}
    monkeypatch.setenv("QWEN_AUDIO_HISTORY_BUDGET_S", "300")
    assert provider.audio_history_budget_s == 300.0


# --- upstream errors ----------------------------------------------------------


@pytest.mark.asyncio
async def test_upstream_error_is_not_forwarded_to_clients(monkeypatch):
    frontend: list[dict] = []

    async def on_event(ev):
        frontend.append(ev)

    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    relay, _ = _relay(provider, on_event)
    relay._ws = FakeWS(inbound=[  # type: ignore[assignment]
        {"type": "response.audio_transcript.delta", "delta": "hi"},
        {"type": "error", "error": {
            "code": "COMMON_ERROR",
            "message": "<400> InternalError.Algo.InvalidParameter: Too many audios. The maximum allowed is 320.",
        }},
    ])
    await relay._drain()

    forwarded = [e.get("type") for e in frontend]
    assert "response.audio_transcript.delta" in forwarded
    raw_errors = [
        e for e in frontend
        if e.get("type") == "error" and (e.get("error") or {}).get("code") == "COMMON_ERROR"
    ]
    assert raw_errors == []
    # The orchestrator pipeline still sees it.
    queued = []
    while not provider._queue.empty():
        queued.append(provider._queue.get_nowait())
    assert any(e.get("type") == "error" for e in queued)


def test_too_many_audios_is_not_classified_as_rate_limit():
    provider = QwenVoiceProvider(model="qwen3.8-omni-flash-realtime")
    err = provider.classify_close_reason(
        Exception("<400> InternalError.Algo.InvalidParameter: Too many audios. The maximum allowed is 320."),
        1007,
        None,
    )
    assert err is not None
    assert err.category == VoiceErrorCategory.NETWORK
    assert err.recoverable is True
