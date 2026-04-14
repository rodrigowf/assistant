"""Audio utilities for format conversion.

OpenAI's audio input only supports wav and mp3 formats. Browser MediaRecorder
typically outputs webm/opus. This module handles conversion using ffmpeg.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Formats that OpenAI accepts directly
OPENAI_SUPPORTED_FORMATS = {"wav", "mp3"}

# Target format for conversion
TARGET_FORMAT = "wav"


def convert_audio_to_wav(
    audio_data: bytes | str,
    source_format: str,
) -> tuple[bytes | str, str]:
    """Convert audio to WAV format if needed.

    Args:
        audio_data: Raw audio bytes or base64-encoded string
        source_format: Source format (e.g., "webm", "ogg", "mp3", "wav")

    Returns:
        Tuple of (audio_data, format) - either original if already supported,
        or converted to WAV format.

    Raises:
        RuntimeError: If ffmpeg conversion fails
    """
    source_format = source_format.lower().lstrip(".")

    # Already in a supported format
    if source_format in OPENAI_SUPPORTED_FORMATS:
        return audio_data, source_format

    # Need to convert - decode base64 if necessary
    if isinstance(audio_data, str):
        audio_bytes = base64.b64decode(audio_data)
        was_base64 = True
    else:
        audio_bytes = audio_data
        was_base64 = False

    # Convert using ffmpeg
    converted_bytes = _ffmpeg_convert(audio_bytes, source_format, TARGET_FORMAT)

    # Return in same format as input (bytes or base64)
    if was_base64:
        return base64.b64encode(converted_bytes).decode("utf-8"), TARGET_FORMAT
    else:
        return converted_bytes, TARGET_FORMAT


def _ffmpeg_convert(
    audio_bytes: bytes,
    source_format: str,
    target_format: str,
) -> bytes:
    """Convert audio using ffmpeg subprocess.

    Args:
        audio_bytes: Raw audio data
        source_format: Input format (for ffmpeg -f flag)
        target_format: Output format

    Returns:
        Converted audio bytes

    Raises:
        RuntimeError: If ffmpeg fails
    """
    # Map format names to ffmpeg format identifiers
    format_map = {
        "webm": "webm",
        "ogg": "ogg",
        "opus": "ogg",  # opus is typically in ogg container
        "mp4": "mp4",
        "m4a": "m4a",
        "wav": "wav",
        "mp3": "mp3",
    }

    input_format = format_map.get(source_format, source_format)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        input_file = tmppath / f"input.{source_format}"
        output_file = tmppath / f"output.{target_format}"

        # Write input
        input_file.write_bytes(audio_bytes)

        # Run ffmpeg
        cmd = [
            "ffmpeg",
            "-y",  # Overwrite output
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(input_file),
            "-ar", "16000",  # 16kHz sample rate (good for speech)
            "-ac", "1",  # Mono
            str(output_file),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=30,
            )

            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")
                logger.error(f"ffmpeg failed: {stderr}")
                raise RuntimeError(f"Audio conversion failed: {stderr}")

            return output_file.read_bytes()

        except subprocess.TimeoutExpired:
            raise RuntimeError("Audio conversion timed out")
        except FileNotFoundError:
            raise RuntimeError("ffmpeg not found - please install ffmpeg")
