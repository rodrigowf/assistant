"""Audio recording for voice sessions.

Records raw PCM audio from voice conversations to disk for later playback.
Captures both user input (mic) and assistant output (speaker) in a single
interleaved file.

Recordings are stored in context/recordings/<session_id>/ with:
- audio.pcm — raw PCM16 24kHz mono, interleaved user and assistant chunks
- index.json — chunk index mapping byte offsets to timestamps and channels

The orchestrator can later play back these recordings using timestamps
from audio_segment entries in conversation history.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.paths import get_context_dir

logger = logging.getLogger(__name__)

# Where recordings land — one subdirectory per session.
RECORDINGS_DIR = get_context_dir() / "recordings"

# Default sample rate for voice providers (PCM16 mono).
DEFAULT_SAMPLE_RATE = 24000


@dataclass
class AudioChunk:
    """A chunk of audio in the interleaved file."""

    channel: str  # "user" or "assistant"
    byte_offset: int  # Where this chunk starts in audio.pcm
    byte_length: int  # Length of this chunk in bytes
    timestamp_ms: int  # Wall-clock offset from session start

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "byte_offset": self.byte_offset,
            "byte_length": self.byte_length,
            "timestamp_ms": self.timestamp_ms,
        }


@dataclass
class AudioSegment:
    """A segment of audio with timing info, linked to a conversation turn."""

    channel: str  # "user" or "assistant"
    start_ms: int  # Offset into the channel's audio stream
    end_ms: int  # End offset
    transcript: str = ""  # Text transcript if available
    turn_index: int = 0  # Which turn in the conversation

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "transcript": self.transcript,
            "turn_index": self.turn_index,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AudioSegment":
        return cls(
            channel=data["channel"],
            start_ms=data["start_ms"],
            end_ms=data["end_ms"],
            transcript=data.get("transcript", ""),
            turn_index=data.get("turn_index", 0),
        )


@dataclass
class RecordingMetadata:
    """Metadata for a voice recording session."""

    session_id: str
    started_at: float  # Unix timestamp
    ended_at: float | None = None
    provider: str = ""
    model: str = ""
    voice: str = ""
    sample_rate: int = DEFAULT_SAMPLE_RATE
    total_bytes: int = 0
    user_duration_ms: int = 0
    assistant_duration_ms: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    chunks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "provider": self.provider,
            "model": self.model,
            "voice": self.voice,
            "sample_rate": self.sample_rate,
            "total_bytes": self.total_bytes,
            "user_duration_ms": self.user_duration_ms,
            "assistant_duration_ms": self.assistant_duration_ms,
            "segments": self.segments,
            "chunks": self.chunks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordingMetadata":
        return cls(
            session_id=data["session_id"],
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            provider=data.get("provider", ""),
            model=data.get("model", ""),
            voice=data.get("voice", ""),
            sample_rate=data.get("sample_rate", DEFAULT_SAMPLE_RATE),
            total_bytes=data.get("total_bytes", 0),
            user_duration_ms=data.get("user_duration_ms", 0),
            assistant_duration_ms=data.get("assistant_duration_ms", 0),
            segments=data.get("segments", []),
            chunks=data.get("chunks", []),
        )


@dataclass
class AudioRecorder:
    """Records user and assistant audio streams for a single voice session.

    All audio is written to a single interleaved file (audio.pcm) with a
    chunk index tracking byte offsets for each source.

    Usage::

        recorder = AudioRecorder(session_id, provider="qwen", model="qwen3.5-omni")
        recorder.start()
        # During the session:
        recorder.write_user_audio(pcm_b64)
        recorder.write_assistant_audio(pcm_b64)
        # When done:
        recorder.stop()
    """

    session_id: str
    provider: str = ""
    model: str = ""
    voice: str = ""
    sample_rate: int = DEFAULT_SAMPLE_RATE

    _dir: Path = field(init=False, repr=False)
    _audio_file: Any = field(init=False, default=None, repr=False)
    _metadata: RecordingMetadata = field(init=False, repr=False)
    _started: bool = field(init=False, default=False, repr=False)
    _start_time: float = field(init=False, default=0.0, repr=False)
    _current_user_segment_start: int = field(init=False, default=-1, repr=False)
    _current_assistant_segment_start: int = field(init=False, default=-1, repr=False)
    _turn_index: int = field(init=False, default=0, repr=False)

    def __post_init__(self) -> None:
        self._dir = RECORDINGS_DIR / self.session_id
        self._metadata = RecordingMetadata(
            session_id=self.session_id,
            started_at=time.time(),
            provider=self.provider,
            model=self.model,
            voice=self.voice,
            sample_rate=self.sample_rate,
        )

    @property
    def is_recording(self) -> bool:
        return self._started and self._audio_file is not None

    @property
    def recording_dir(self) -> Path:
        return self._dir

    def start(self) -> None:
        """Begin recording. Creates the output directory and file."""
        if self._started:
            return

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._audio_file = open(self._dir / "audio.pcm", "wb")
            self._start_time = time.time()
            self._started = True
            logger.info(
                "audio_recorder started session_id=%s dir=%s",
                self.session_id,
                self._dir,
            )
        except Exception:
            logger.exception("Failed to start audio recorder for %s", self.session_id)
            self._cleanup()

    def _elapsed_ms(self) -> int:
        """Milliseconds since recording started."""
        return int((time.time() - self._start_time) * 1000)

    def write_user_audio(self, pcm_b64: str) -> None:
        """Write a base64-encoded PCM chunk from the user's microphone."""
        if not self._started or self._audio_file is None:
            return
        try:
            # Track segment start if this is first chunk of a new segment
            if self._current_user_segment_start < 0:
                self._current_user_segment_start = self._metadata.user_duration_ms

            pcm_bytes = base64.b64decode(pcm_b64)
            byte_offset = self._metadata.total_bytes

            # Write to interleaved file
            self._audio_file.write(pcm_bytes)

            # Record chunk in index
            chunk = AudioChunk(
                channel="user",
                byte_offset=byte_offset,
                byte_length=len(pcm_bytes),
                timestamp_ms=self._elapsed_ms(),
            )
            self._metadata.chunks.append(chunk.to_dict())
            self._metadata.total_bytes += len(pcm_bytes)

            # Track user duration (PCM16 mono: 2 bytes per sample)
            self._metadata.user_duration_ms += int(
                (len(pcm_bytes) / 2 / self.sample_rate) * 1000
            )
        except Exception:
            logger.exception("Failed to write user audio for %s", self.session_id)

    def write_assistant_audio(self, pcm_b64: str) -> None:
        """Write a base64-encoded PCM chunk from the assistant's response."""
        if not self._started or self._audio_file is None:
            return
        try:
            # Track segment start if this is first chunk of a new segment
            if self._current_assistant_segment_start < 0:
                self._current_assistant_segment_start = self._metadata.assistant_duration_ms

            pcm_bytes = base64.b64decode(pcm_b64)
            byte_offset = self._metadata.total_bytes

            # Write to interleaved file
            self._audio_file.write(pcm_bytes)

            # Record chunk in index
            chunk = AudioChunk(
                channel="assistant",
                byte_offset=byte_offset,
                byte_length=len(pcm_bytes),
                timestamp_ms=self._elapsed_ms(),
            )
            self._metadata.chunks.append(chunk.to_dict())
            self._metadata.total_bytes += len(pcm_bytes)

            # Track assistant duration
            self._metadata.assistant_duration_ms += int(
                (len(pcm_bytes) / 2 / self.sample_rate) * 1000
            )
        except Exception:
            logger.exception("Failed to write assistant audio for %s", self.session_id)

    def mark_user_turn_end(self, transcript: str = "") -> dict[str, Any] | None:
        """Mark the end of a user speech segment. Returns segment info for JSONL."""
        if self._current_user_segment_start < 0:
            return None

        segment = AudioSegment(
            channel="user",
            start_ms=self._current_user_segment_start,
            end_ms=self._metadata.user_duration_ms,
            transcript=transcript,
            turn_index=self._turn_index,
        )
        self._metadata.segments.append(segment.to_dict())
        self._current_user_segment_start = -1
        self._turn_index += 1
        return segment.to_dict()

    def mark_assistant_turn_end(self, transcript: str = "") -> dict[str, Any] | None:
        """Mark the end of an assistant speech segment. Returns segment info for JSONL."""
        if self._current_assistant_segment_start < 0:
            return None

        segment = AudioSegment(
            channel="assistant",
            start_ms=self._current_assistant_segment_start,
            end_ms=self._metadata.assistant_duration_ms,
            transcript=transcript,
            turn_index=self._turn_index,
        )
        self._metadata.segments.append(segment.to_dict())
        self._current_assistant_segment_start = -1
        self._turn_index += 1
        return segment.to_dict()

    def stop(self) -> None:
        """Finalize the recording and write metadata."""
        if not self._started:
            return

        self._metadata.ended_at = time.time()
        self._write_metadata()
        self._cleanup()
        self._started = False

        logger.info(
            "audio_recorder stopped session_id=%s total=%d bytes user=%.1fs assistant=%.1fs",
            self.session_id,
            self._metadata.total_bytes,
            self._metadata.user_duration_ms / 1000,
            self._metadata.assistant_duration_ms / 1000,
        )

    def _write_metadata(self) -> None:
        """Write the metadata/index JSON file."""
        try:
            with open(self._dir / "index.json", "w") as f:
                json.dump(self._metadata.to_dict(), f, indent=2)
        except Exception:
            logger.exception("Failed to write metadata for %s", self.session_id)

    def _cleanup(self) -> None:
        """Close file handles."""
        if self._audio_file is not None:
            try:
                self._audio_file.close()
            except Exception:
                pass
            self._audio_file = None


# --- Utility functions for the orchestrator playback tool ---


def get_recording(session_id: str) -> dict[str, Any] | None:
    """Get metadata for a specific recording."""
    session_dir = RECORDINGS_DIR / session_id
    index_file = session_dir / "index.json"
    if not index_file.exists():
        return None
    try:
        with open(index_file) as f:
            meta = json.load(f)
        meta["has_audio"] = (session_dir / "audio.pcm").exists()
        return meta
    except Exception:
        return None


def get_recording_audio(
    session_id: str,
    channel: str,
    start_ms: int,
    duration_ms: int,
) -> bytes | None:
    """Read raw PCM audio for a specific channel and time range.

    Uses the chunk index to find and concatenate the relevant portions
    of the interleaved audio file.

    Args:
        session_id: The session to read from
        channel: "user" or "assistant"
        start_ms: Start position in channel's audio stream (milliseconds)
        duration_ms: Duration to read (milliseconds)

    Returns:
        Raw PCM16 bytes for the requested segment, or None on error.
    """
    session_dir = RECORDINGS_DIR / session_id
    audio_file = session_dir / "audio.pcm"
    index_file = session_dir / "index.json"

    if not audio_file.exists() or not index_file.exists():
        return None

    try:
        with open(index_file) as f:
            meta = json.load(f)
    except Exception:
        return None

    sample_rate = meta.get("sample_rate", DEFAULT_SAMPLE_RATE)
    chunks = meta.get("chunks", [])

    # Filter chunks for this channel
    channel_chunks = [c for c in chunks if c["channel"] == channel]
    if not channel_chunks:
        return None

    # Calculate byte range needed
    bytes_per_ms = (sample_rate * 2) / 1000
    start_byte_in_channel = int(start_ms * bytes_per_ms)
    end_byte_in_channel = int((start_ms + duration_ms) * bytes_per_ms)

    # Walk through channel chunks to find relevant portions
    result = bytearray()
    channel_position = 0  # Current position in the channel's audio stream

    try:
        with open(audio_file, "rb") as f:
            for chunk in channel_chunks:
                chunk_start = channel_position
                chunk_end = channel_position + chunk["byte_length"]

                # Skip if chunk is entirely before our range
                if chunk_end <= start_byte_in_channel:
                    channel_position = chunk_end
                    continue

                # Stop if chunk is entirely after our range
                if chunk_start >= end_byte_in_channel:
                    break

                # Calculate overlap
                read_start = max(0, start_byte_in_channel - chunk_start)
                read_end = min(chunk["byte_length"], end_byte_in_channel - chunk_start)

                # Read the relevant portion
                f.seek(chunk["byte_offset"] + read_start)
                data = f.read(read_end - read_start)
                result.extend(data)

                channel_position = chunk_end

                # Stop if we've read enough
                if channel_position >= end_byte_in_channel:
                    break

    except Exception:
        logger.exception("Failed to read audio from %s", audio_file)
        return None

    return bytes(result)


def is_recording_enabled() -> bool:
    """Check if voice recording is enabled in the config."""
    from api.routes.config import _load_config
    config = _load_config()
    return config.get("voice_recording_enabled", False)
