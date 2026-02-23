#!/usr/bin/env python3
"""
Usage: context/scripts/index-memory.py [options]
Description: Index memory and session history into the vector store.

This indexes from the context/ directory:
  - context/memory/*.md -> 'memory' collection
  - context/*.jsonl -> 'history' collection

Options:
    --memory-only    Only re-index memory files
    --history-only   Only re-index session history
    --reset          Clear collections before indexing

Examples:
    context/scripts/index-memory.py
    context/scripts/index-memory.py --memory-only
    context/scripts/index-memory.py --reset
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

# Add project root to path for utils import
SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from utils.paths import get_memory_dir, get_sessions_dir, get_index_dir

EMBED_SCRIPT = SCRIPT_DIR / "embed.py"


def run_embed(command: str, *args) -> bool:
    """Run embed.py with given arguments."""
    cmd = [sys.executable, str(EMBED_SCRIPT), command, *args]
    result = subprocess.run(cmd, capture_output=False)
    return result.returncode == 0


def extract_session_text(jsonl_path: Path) -> str:
    """Extract human-readable text from a session JSONL file."""
    lines = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue

                # Extract text content
                msg = obj.get("message", {})
                content = msg.get("content", "")

                if isinstance(content, str):
                    text = content
                else:
                    # Content is a list of blocks
                    text_parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                    text = "\n".join(text_parts)

                if text.strip():
                    role = "User" if msg_type == "user" else "Assistant"
                    lines.append(f"## {role}\n\n{text}\n")
    except (OSError, PermissionError):
        pass

    return "\n".join(lines)


def index_memory(reset: bool = False) -> None:
    """Index memory files from context/memory/."""
    memory_dir = get_memory_dir()

    if not memory_dir.exists():
        print(f"Memory directory not found: {memory_dir}")
        print("(This is normal if no memory files exist yet)")
        return

    md_files = list(memory_dir.glob("*.md"))
    if not md_files:
        print("No memory files found, skipping")
        return

    print(f"=== Indexing {len(md_files)} memory files ===")
    if reset:
        run_embed("reset", "--collection", "memory")

    run_embed("index", str(memory_dir), "--collection", "memory")


def index_history(reset: bool = False) -> None:
    """Index session JSONL files from context/."""
    sessions_dir = get_sessions_dir()

    if not sessions_dir.exists():
        print(f"Sessions directory not found: {sessions_dir}")
        return

    jsonl_files = list(sessions_dir.glob("*.jsonl"))
    if not jsonl_files:
        print("No session files found, skipping")
        return

    print(f"=== Indexing {len(jsonl_files)} session files ===")
    if reset:
        run_embed("reset", "--collection", "history")

    # Convert JSONL files to temporary markdown for embedding
    temp_dir = PROJECT_DIR / ".index-temp"
    temp_dir.mkdir(exist_ok=True)

    try:
        for jsonl_path in jsonl_files:
            text = extract_session_text(jsonl_path)
            if text.strip():
                # Use session ID as filename
                md_path = temp_dir / f"{jsonl_path.stem}.md"
                md_path.write_text(f"# Session: {jsonl_path.stem}\n\n{text}")

        # Index the temp directory
        if any(temp_dir.iterdir()):
            run_embed("index", str(temp_dir), "--collection", "history")
    finally:
        # Clean up temp files
        for f in temp_dir.glob("*"):
            f.unlink()
        temp_dir.rmdir()


def main():
    parser = argparse.ArgumentParser(description="Index memory and history")
    parser.add_argument("--memory-only", action="store_true", help="Only index memory/")
    parser.add_argument("--history-only", action="store_true", help="Only index history/")
    parser.add_argument("--reset", action="store_true", help="Clear collections first")
    args = parser.parse_args()

    do_memory = not args.history_only
    do_history = not args.memory_only

    memory_dir = get_memory_dir()
    sessions_dir = get_sessions_dir()
    index_dir = get_index_dir()

    print(f"Memory dir:   {memory_dir}")
    print(f"Sessions dir: {sessions_dir}")
    print(f"Index dir:    {index_dir}\n")

    if do_memory:
        index_memory(reset=args.reset)

    if do_history:
        index_history(reset=args.reset)

    print("\n=== Stats ===")
    run_embed("stats", "--collection", "memory")
    run_embed("stats", "--collection", "history")


if __name__ == "__main__":
    main()
