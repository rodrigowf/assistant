"""Audio playback tool — let the orchestrator hear past voice conversations.

The orchestrator sees audio_segment entries in conversation history with
timestamps (start_ms, end_ms) pointing into stored recordings. This tool
lets the orchestrator inject those segments into the active voice session,
so the real-time model can "hear" past speech.

Use case: During a voice conversation, the orchestrator can replay past
emotional moments by feeding stored audio into the live WebSocket stream.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from orchestrator.audio_recorder import (
    get_recording,
    get_recording_audio_by_wall_clock,
)
from orchestrator.tools import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="listen_recording",
    description=(
        "Replay a wall-clock time range from a past voice session INTO the active "
        "real-time conversation, so the model can 'hear' both sides of a previous "
        "conversation in their natural chronological order.\n\n"
        "Conversation history contains markers like "
        "[voice, recording: <session_id> <start_ms>-<end_ms>ms]. Pass those values "
        "to replay that range. Both user and assistant audio are included, "
        "interleaved exactly as they happened. Silences between turns were never "
        "recorded (VAD-stripped), so playback is dense — no padding.\n\n"
        "To replay a full conversation, pass start_ms=0 and end_ms set high enough "
        "to cover the whole recording (use peek_history or the recording's metadata "
        "to find the duration).\n\n"
        "REQUIREMENTS:\n"
        "- Active voice session using a WebSocket provider (Qwen, Gemini)\n"
        "- The voice relay must be running"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID of the stored recording (from the history marker)",
            },
            "start_ms": {
                "type": "integer",
                "description": "Wall-clock start in milliseconds (ms since session start)",
                "minimum": 0,
            },
            "end_ms": {
                "type": "integer",
                "description": "Wall-clock end in milliseconds (ms since session start)",
                "minimum": 0,
            },
        },
        "required": ["session_id", "start_ms", "end_ms"],
    },
)
async def listen_recording(
    context: dict,
    session_id: str,
    start_ms: int,
    end_ms: int,
) -> str:
    """Inject stored audio into the active voice session.

    Feeds past recordings into the live WebSocket stream so the real-time
    model can hear them. Both channels (user + assistant) play back
    interleaved in wall-clock order.
    """
    # Validate time range
    if end_ms <= start_ms:
        return json.dumps({"error": "end_ms must be greater than start_ms."})

    duration_ms = end_ms - start_ms
    # 5-minute cap. Recording is silence-stripped already, so even a long
    # wall-clock window stays well under this in actual bytes.
    if duration_ms > 300000:
        return json.dumps({"error": "Maximum range is 5 minutes (300000ms)."})

    # Get the orchestrator session from context
    session = context.get("session")
    if session is None:
        return json.dumps({
            "error": "No orchestrator session found.",
            "hint": "This tool requires an active orchestrator session.",
        })

    # Check if voice mode is active with a relay
    if not getattr(session, "is_voice", False):
        return json.dumps({
            "error": "Voice mode is not active.",
            "hint": "Start a voice session first.",
        })

    if not getattr(session, "needs_voice_relay", False):
        return json.dumps({
            "error": "Voice provider does not use WebSocket relay.",
            "hint": "Audio injection only works with WebSocket providers (Qwen, Gemini).",
        })

    # Check if the voice relay is running
    voice_relay = getattr(session, "_voice_relay", None)
    if voice_relay is None or not voice_relay.is_running:
        return json.dumps({
            "error": "Voice relay is not running.",
            "hint": "The voice WebSocket connection must be active.",
        })

    # Get recording metadata
    recording = get_recording(session_id)
    if recording is None:
        return json.dumps({"error": f"Recording not found: {session_id}"})

    # Check if the recording has audio
    if not recording.get("has_audio", False):
        return json.dumps({"error": "Recording has no audio file."})

    # Get the audio bytes — both channels in wall-clock order.
    audio_bytes = get_recording_audio_by_wall_clock(session_id, start_ms, end_ms)
    if audio_bytes is None:
        return json.dumps({"error": "Failed to read audio data."})

    if len(audio_bytes) == 0:
        return json.dumps({
            "error": "No audio in that range.",
            "hint": (
                "The range may fall in a silence between turns "
                "(silences are not recorded), or be outside the recording's "
                "duration."
            ),
        })

    # Get sample rate and calculate chunk size
    sample_rate = recording.get("sample_rate", 24000)
    chunk_duration_ms = 100  # 100ms chunks
    bytes_per_ms = (sample_rate * 2) // 1000
    chunk_size_bytes = (chunk_duration_ms * bytes_per_ms // 2) * 2  # Align to sample

    # Calculate actual duration
    actual_duration_ms = (len(audio_bytes) / 2 / sample_rate) * 1000

    # Arm the injection window — defence in depth against any phantom
    # event that slips past the provider-side VAD-off (timing race, older
    # provider, etc).  See OrchestratorSession.is_injecting.  The grace
    # must outlast the provider's ASR completion delay — Qwen has been
    # observed firing transcription.completed events ~10s after the last
    # input_audio_buffer.append.
    INJECTION_GRACE_SEC = 15.0
    audio_duration_sec = actual_duration_ms / 1000.0
    session.extend_injection_window(audio_duration_sec + INJECTION_GRACE_SEC)

    # Disable server VAD for the duration of the injection.  Without this,
    # Qwen's VAD chops the replayed audio at every natural pause and fires
    # speech_started / transcription.completed (and an auto-created
    # response) for each fragment — which then interrupts itself
    # repeatedly because each new speech_started cancels the previous
    # response.  By turning VAD off we make the provider treat the whole
    # injection as one item that we explicitly commit at the end.
    provider = getattr(session, "_voice_provider", None)
    disable_vad = getattr(provider, "session_update_disable_vad", None) if provider else None
    restore_vad = getattr(provider, "session_update_restore_vad", None) if provider else None
    commit_buffer = getattr(provider, "commit_input_audio", None) if provider else None
    vad_was_disabled = False
    if disable_vad is not None:
        try:
            await session.send_voice_event_upstream(disable_vad())
            vad_was_disabled = True
        except Exception:  # noqa: BLE001
            logger.exception("Failed to disable VAD before injection")

    # Stream chunks to the voice session
    chunks_sent = 0
    bytes_sent = 0

    try:
        offset = 0
        # Re-extend the window periodically during long playbacks so the
        # deadline always sits comfortably ahead of the pacing.
        chunks_per_extension = max(1, int(2000 / chunk_duration_ms))  # every ~2s
        while offset < len(audio_bytes):
            chunk = audio_bytes[offset:offset + chunk_size_bytes]
            if not chunk:
                break

            chunk_b64 = base64.b64encode(chunk).decode("ascii")
            await session.send_voice_audio_in(chunk_b64)

            chunks_sent += 1
            bytes_sent += len(chunk)
            offset += chunk_size_bytes

            if chunks_sent % chunks_per_extension == 0:
                remaining_bytes = max(0, len(audio_bytes) - offset)
                remaining_sec = (remaining_bytes / 2 / sample_rate)
                session.extend_injection_window(remaining_sec + INJECTION_GRACE_SEC)

            # Pace at ~80% real-time to avoid overwhelming the stream
            await asyncio.sleep(chunk_duration_ms * 0.0008)

        # Final extension covers ASR catching up after the last chunk.
        session.extend_injection_window(INJECTION_GRACE_SEC)

        # With VAD disabled, the provider auto-commit doesn't fire — send
        # the commit ourselves so the model actually sees the buffered
        # audio as one user item (and produces a single transcription).
        if vad_was_disabled and commit_buffer is not None:
            try:
                await session.send_voice_event_upstream(commit_buffer())
            except Exception:  # noqa: BLE001
                logger.exception("Failed to commit input_audio_buffer after injection")

        logger.info(
            "listen_recording complete recording=%s %d-%dms chunks=%d",
            session_id[:8],
            start_ms,
            end_ms,
            chunks_sent,
        )

        return json.dumps({
            "success": True,
            "session_id": session_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "audio_duration_ms": actual_duration_ms,
            "bytes_sent": bytes_sent,
        })

    except Exception as e:
        # Even on failure, hold the suppression window briefly so any
        # already-in-flight transcription events don't slip through.
        try:
            session.extend_injection_window(5.0)
        except Exception:  # noqa: BLE001
            pass
        logger.exception("Failed to inject audio")
        return json.dumps({
            "error": f"Audio injection failed: {str(e)}",
            "bytes_sent_before_failure": bytes_sent,
        })

    finally:
        # Always restore VAD, even on error — leaving it disabled would
        # break the next user turn (Qwen would never fire speech_started).
        if vad_was_disabled and restore_vad is not None:
            try:
                await session.send_voice_event_upstream(restore_vad())
            except Exception:  # noqa: BLE001
                logger.exception("Failed to restore VAD after injection")
