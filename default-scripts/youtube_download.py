#!/usr/bin/env python3
"""
Usage: youtube_download.py <url> [--mode video|audio|both] [--output-dir DIR]
                                   [--audio-format wav|flac|mp3|opus]
                                   [--video-quality best|1080p|720p|480p]
                                   [--keep-source]

Description: Download a YouTube video and/or extract audio for content creation.

Modes:
  video  — Download merged video (default container picked by ffmpeg capability)
  audio  — Download and extract audio only
  both   — Download video AND extract audio (default)

Examples:
  youtube_download.py "https://youtu.be/KE6qgEZi4yU"
  youtube_download.py "https://youtu.be/KE6qgEZi4yU" --mode video --video-quality 1080p
  youtube_download.py "https://youtu.be/KE6qgEZi4yU" --mode audio --audio-format wav
  youtube_download.py "https://youtu.be/KE6qgEZi4yU" -o ~/projects/myvideo --keep-source

Notes:
  - Avoid output-dir inside `context/` — context-sync interferes with downloads.
  - On the Jetson (ffmpeg < 4.3) Opus-in-MP4 is not supported, so the script
    automatically falls back to MKV when picking the best audio (Opus 153kbps).
    On the Laptop (ffmpeg >= 4.3) MP4 with Opus works fine.
  - Audio defaults to WAV (PCM 16-bit, 48kHz stereo) — Reaper-ready.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def find_yt_dlp() -> str:
    """Find yt-dlp binary. Prefers system path, falls back to ~/.local/bin."""
    for candidate in ("yt-dlp", str(Path.home() / ".local/bin/yt-dlp")):
        if shutil.which(candidate) or Path(candidate).is_file():
            return candidate
    sys.exit(
        "Error: yt-dlp not found. Install it with `pipx install yt-dlp` "
        "or `pip install --user yt-dlp`."
    )


def ffmpeg_major_version() -> int:
    """Return the major version of ffmpeg (e.g. 3, 4, 6)."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, check=True
        ).stdout
        match = re.search(r"ffmpeg version (\d+)", out)
        return int(match.group(1)) if match else 0
    except (FileNotFoundError, subprocess.CalledProcessError):
        sys.exit("Error: ffmpeg not found. Install it via your package manager.")


def height_filter(quality: str) -> str:
    """Build yt-dlp height constraint, accounting for portrait videos.

    For portrait videos `height` refers to the long edge, so we constrain by
    the short edge via `width<=N`. yt-dlp accepts both, so we OR them together.
    """
    if quality == "best":
        return ""
    n = quality.rstrip("p")
    return f"[height<={n}][width<={n}]"


def build_video_format(quality: str) -> str:
    """Format selector for best H.264 video (most editor-compatible)."""
    constraint = height_filter(quality)
    return (
        f"bestvideo{constraint}[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/"
        f"best{constraint}[ext=mp4]"
    )


def build_best_format(quality: str, ffmpeg_major: int) -> str:
    """Best video + best audio. On old ffmpeg, force AAC for MP4 compatibility."""
    constraint = height_filter(quality)
    if ffmpeg_major >= 4:
        return (
            f"bestvideo{constraint}[ext=mp4][vcodec^=avc]+bestaudio/"
            f"bestvideo{constraint}+bestaudio/best{constraint}"
        )
    return build_video_format(quality)


def pick_container(ffmpeg_major: int) -> str:
    """MP4 if ffmpeg supports Opus-in-MP4, else MKV."""
    return "mp4" if ffmpeg_major >= 4 else "mkv"


def get_video_info(url: str, yt_dlp: str) -> dict:
    """Extract metadata via yt-dlp --dump-json."""
    proc = subprocess.run(
        [yt_dlp, "--dump-json", "--no-warnings", url],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def download_video(
    url: str,
    output_dir: Path,
    quality: str,
    ffmpeg_major: int,
    yt_dlp: str,
) -> Path:
    """Download merged video. Returns the output path."""
    container = pick_container(ffmpeg_major)
    fmt = build_best_format(quality, ffmpeg_major)
    output_template = str(output_dir / "%(title)s_%(id)s.%(ext)s")

    cmd = [
        yt_dlp,
        "--no-part",
        "-f", fmt,
        "--merge-output-format", container,
        "-o", output_template,
        "--no-write-info-json",
        url,
    ]
    print(f"Downloading video → {container.upper()} ({quality})…", flush=True)
    subprocess.run(cmd, check=True)

    info = get_video_info(url, yt_dlp)
    title = info["title"]
    vid = info["id"]
    safe_title = re.sub(r"[/\\]", "_", title)
    return output_dir / f"{safe_title}_{vid}.{container}"


AUDIO_CODEC_MAP = {
    "wav": ("pcm_s16le", "wav", ["-ar", "48000", "-ac", "2"]),
    "flac": ("flac", "flac", ["-ar", "48000"]),
    "mp3": ("libmp3lame", "mp3", ["-b:a", "320k"]),
    "opus": (None, "opus", []),  # Direct stream copy from source
}


def extract_audio(
    video_path: Path,
    audio_format: str,
) -> Path:
    """Extract audio track from video using ffmpeg."""
    if audio_format not in AUDIO_CODEC_MAP:
        sys.exit(f"Error: unsupported audio format: {audio_format}")

    codec, ext, extra_args = AUDIO_CODEC_MAP[audio_format]
    audio_path = video_path.with_suffix(f".{ext}")

    cmd = ["ffmpeg", "-y", "-i", str(video_path), "-vn"]
    if codec is None:
        cmd += ["-acodec", "copy"]
    else:
        cmd += ["-acodec", codec] + extra_args
    cmd += [str(audio_path)]

    print(f"Extracting audio → {audio_format.upper()}…", flush=True)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return audio_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download a YouTube video and/or extract audio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", help="YouTube URL")
    parser.add_argument(
        "--mode",
        choices=["video", "audio", "both"],
        default="both",
        help="What to produce (default: both)",
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path.cwd(),
        help="Output directory (default: current working dir)",
    )
    parser.add_argument(
        "--audio-format",
        choices=list(AUDIO_CODEC_MAP),
        default="wav",
        help="Audio format for --mode audio/both (default: wav, Reaper-ready)",
    )
    parser.add_argument(
        "--video-quality",
        choices=["best", "2160p", "1440p", "1080p", "720p", "480p"],
        default="best",
        help="Max video quality (default: best)",
    )
    parser.add_argument(
        "--keep-source",
        action="store_true",
        help="In audio-only mode, keep the downloaded video file too",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.expanduser().resolve()
    if "context/" in str(output_dir):
        print(
            "Warning: output-dir is inside context/ — context-sync may interfere "
            "with downloads. Consider a path outside context/.",
            file=sys.stderr,
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    yt_dlp = find_yt_dlp()
    ffmpeg_major = ffmpeg_major_version()

    video_path = download_video(
        args.url, output_dir, args.video_quality, ffmpeg_major, yt_dlp
    )
    if not video_path.exists():
        sys.exit(f"Error: expected output not found at {video_path}")

    print(f"  → {video_path}")

    audio_path = None
    if args.mode in ("audio", "both"):
        audio_path = extract_audio(video_path, args.audio_format)
        print(f"  → {audio_path}")

    if args.mode == "audio" and not args.keep_source:
        video_path.unlink()
        print(f"  removed source video (use --keep-source to retain)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
