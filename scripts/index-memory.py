#!/usr/bin/env python3
"""
Usage: scripts/index-memory.py [options]
Description: Index Claude Code memory and session history into the vector store.

This indexes from Claude Code's native storage locations:
  - $CLAUDE_CONFIG_DIR/projects/<project>/memory/*.md -> 'memory' collection
  - $CLAUDE_CONFIG_DIR/projects/<project>/*.jsonl -> 'history' collection

If CLAUDE_CONFIG_DIR is not set, defaults to ~/.claude.

Options:
    --memory-only    Only re-index memory files
    --history-only   Only re-index session history
    --reset          Clear collections before indexing

Examples:
    scripts/index-memory.py
    scripts/index-memory.py --memory-only
    scripts/index-memory.py --reset
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_DIR = SCRIPT_DIR.parent
EMBED_SCRIPT = SCRIPT_DIR / "embed.py"


def get_claude_config_dir() -> Path:
    """Get Claude Code config directory from env or default."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir)
    return Path.home() / ".claude"


def get_project_data_dir() -> Path:
    """Get the Claude Code data directory for this project."""
    config_dir = get_claude_config_dir()
    # Mangle the project path the same way Claude Code does
    project_path = str(PROJECT_DIR).replace("/", "-")
    return config_dir / "projects" / project_path


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
    """Index Claude Code's auto-memory files."""
    data_dir = get_project_data_dir()
    memory_dir = data_dir / "memory"

    if not memory_dir.exists():
        print(f"Memory directory not found: {memory_dir}")
        print("(This is normal if Claude Code hasn't created memory yet)")
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
    """Index Claude Code session JSONL files."""
    data_dir = get_project_data_dir()

    if not data_dir.exists():
        print(f"Project data directory not found: {data_dir}")
        return

    jsonl_files = list(data_dir.glob("*.jsonl"))
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
    parser = argparse.ArgumentParser(description="Index Claude Code memory and history")
    parser.add_argument("--memory-only", action="store_true", help="Only index memory/")
    parser.add_argument("--history-only", action="store_true", help="Only index history/")
    parser.add_argument("--reset", action="store_true", help="Clear collections first")
    args = parser.parse_args()

    do_memory = not args.history_only
    do_history = not args.memory_only

    config_dir = get_claude_config_dir()
    data_dir = get_project_data_dir()
    print(f"Claude config: {config_dir}")
    print(f"Project data:  {data_dir}\n")

    if do_memory:
        index_memory(reset=args.reset)

    if do_history:
        index_history(reset=args.reset)

    print("\n=== Stats ===")
    run_embed("stats", "--collection", "memory")
    run_embed("stats", "--collection", "history")


if __name__ == "__main__":
    main()
