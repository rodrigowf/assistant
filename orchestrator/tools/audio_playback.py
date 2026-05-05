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
    get_recording_audio,
)
from orchestrator.tools import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="listen_recording",
    description=(
        "Inject audio from a past voice session INTO the active real-time conversation. "
        "Use this to 'hear' speech from previous conversations.\n\n"
        "The conversation history contains audio_segment entries with timestamps "
        "(start_ms, end_ms, channel) pointing into stored recordings. Pass the "
        "session_id and timestamps from those entries to replay that audio.\n\n"
        "REQUIREMENTS:\n"
        "- There must be an active voice session using a WebSocket provider\n"
        "- The voice relay must be running\n\n"
        "The audio is streamed into the voice input buffer. The real-time model "
        "will process it as if someone had spoken it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "The session ID of the stored recording (from audio_segment in history)",
            },
            "channel": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": "Which channel: 'user' for user speech, 'assistant' for assistant speech",
            },
            "start_ms": {
                "type": "integer",
                "description": "Start position in milliseconds (from audio_segment.start_ms)",
                "minimum": 0,
            },
            "end_ms": {
                "type": "integer",
                "description": "End position in milliseconds (from audio_segment.end_ms)",
                "minimum": 0,
            },
        },
        "required": ["session_id", "channel", "start_ms", "end_ms"],
    },
)
async def listen_recording(
    context: dict,
    session_id: str,
    channel: str,
    start_ms: int,
    end_ms: int,
) -> str:
    """Inject stored audio into the active voice session.

    Feeds past recordings into the live WebSocket stream so the real-time
    model can hear them.
    """
    # Validate channel
    if channel not in ("user", "assistant"):
        return json.dumps({"error": f"Invalid channel: {channel}. Must be 'user' or 'assistant'."})

    # Validate time range
    if end_ms <= start_ms:
        return json.dumps({"error": "end_ms must be greater than start_ms."})

    duration_ms = end_ms - start_ms
    if duration_ms > 60000:
        return json.dumps({"error": "Maximum duration is 60 seconds (60000ms)."})

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

    # Get the audio bytes
    audio_bytes = get_recording_audio(session_id, channel, start_ms, duration_ms)
    if audio_bytes is None:
        return json.dumps({"error": "Failed to read audio data."})

    if len(audio_bytes) == 0:
        return json.dumps({"error": "Audio segment is empty."})

    # Get sample rate and calculate chunk size
    sample_rate = recording.get("sample_rate", 24000)
    chunk_duration_ms = 100  # 100ms chunks
    bytes_per_ms = (sample_rate * 2) // 1000
    chunk_size_bytes = (chunk_duration_ms * bytes_per_ms // 2) * 2  # Align to sample

    # Calculate actual duration
    actual_duration_ms = (len(audio_bytes) / 2 / sample_rate) * 1000

    # Arm the injection window before sending the first byte.  See
    # OrchestratorSession.is_injecting for what the window suppresses.
    # The grace must outlast both the pacing AND the provider's own ASR
    # completion delay — Qwen has been observed firing
    # transcription.completed events ~10s after the last
    # input_audio_buffer.append.
    INJECTION_GRACE_SEC = 15.0
    audio_duration_sec = actual_duration_ms / 1000.0
    session.extend_injection_window(audio_duration_sec + INJECTION_GRACE_SEC)

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

        logger.info(
            "listen_recording complete recording=%s channel=%s %d-%dms chunks=%d",
            session_id[:8],
            channel,
            start_ms,
            end_ms,
            chunks_sent,
        )

        return json.dumps({
            "success": True,
            "session_id": session_id,
            "channel": channel,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": actual_duration_ms,
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
