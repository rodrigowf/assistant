"""Export a stored voice recording to MP3 by replaying the same byte
range the ``listen_recording`` tool would deliver to the model.

Usage::

    context/scripts/run.sh default-scripts/recording_to_mp3.py <session_id> [out.mp3] [start_ms] [end_ms]

Defaults: full duration (start_ms=0, end_ms=last chunk's timestamp + length).
Output path defaults to ``<session_id>.mp3`` in cwd.
"""
from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

# Allow running from anywhere — point at the project root so `orchestrator`
# resolves regardless of the caller's working directory.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from orchestrator.audio_recorder import (
    RECORDINGS_DIR,
    get_recording,
    get_recording_audio_by_wall_clock,
)


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw PCM16 mono in a WAV container (in-memory)."""
    bits_per_sample = 16
    channels = 1
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm)
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
    )
    data_chunk = struct.pack("<4sI", b"data", data_size) + pcm
    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    riff = struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE")
    return riff + fmt_chunk + data_chunk


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    sid = argv[1]
    out_path = Path(argv[2]) if len(argv) >= 3 else Path(f"{sid}.mp3")

    meta = get_recording(sid)
    if meta is None:
        print(f"ERROR: no recording found at {RECORDINGS_DIR / sid}")
        return 1
    if not meta.get("has_audio"):
        print(f"ERROR: recording exists but has no audio.pcm")
        return 1

    sample_rate = meta.get("sample_rate", 24000)
    chunks = meta.get("chunks", [])
    if not chunks:
        print("ERROR: index has no chunks")
        return 1

    last = chunks[-1]
    bytes_per_ms = (sample_rate * 2) / 1000
    last_ms_len = last["byte_length"] / bytes_per_ms
    natural_end = int(last["timestamp_ms"] + last_ms_len) + 1

    start_ms = int(argv[3]) if len(argv) >= 4 else 0
    end_ms = int(argv[4]) if len(argv) >= 5 else natural_end

    print(f"recording: {sid}")
    print(f"sample_rate: {sample_rate} Hz")
    print(f"chunks: {len(chunks)}, segments: {len(meta.get('segments', []))}")
    print(f"wall-clock range: {start_ms}–{end_ms}ms ({(end_ms - start_ms)/1000:.1f}s window)")

    pcm = get_recording_audio_by_wall_clock(sid, start_ms, end_ms)
    if pcm is None:
        print("ERROR: failed to read PCM")
        return 1
    actual_audio_sec = len(pcm) / 2 / sample_rate
    print(f"actual audio: {len(pcm)} bytes ({actual_audio_sec:.1f}s — silences are stripped)")

    wav = pcm_to_wav(pcm, sample_rate)

    print(f"encoding to MP3 → {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "wav", "-i", "pipe:0",
            "-codec:a", "libmp3lame", "-q:a", "4",
            str(out_path),
        ],
        input=wav,
        capture_output=True,
    )
    if proc.returncode != 0:
        print(f"ffmpeg failed: {proc.stderr.decode(errors='replace')}")
        return 1
    size_kb = out_path.stat().st_size / 1024
    print(f"done: {out_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
