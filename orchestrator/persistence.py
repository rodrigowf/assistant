"""JSONL persistence utilities for orchestrator sessions.

Handles reading and writing conversation history to JSONL files, supporting
both text and voice mode sessions with proper reconstruction of tool calls
and results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class HistoryLoader:
    """Loads conversation history from JSONL files.

    Reconstructs the full conversation history including:
    - User messages from text and voice modes
    - Assistant responses with embedded tool calls
    - Tool results grouped as user messages
    """

    def __init__(self, jsonl_path: Path) -> None:
        self._jsonl_path = jsonl_path

    def load(self) -> list[dict[str, Any]]:
        """Load and reconstruct conversation history from JSONL.

        Returns a list of message dictionaries in Anthropic API format:
        - {"role": "user", "content": "text"}
        - {"role": "assistant", "content": [{"type": "text", "text": "..."}, ...]}
        - {"role": "user", "content": [{"type": "tool_result", ...}, ...]}
        """
        if not self._jsonl_path.is_file():
            return []

        entries = self._read_jsonl()
        history = self._reconstruct_history(entries)
        return history

    def _read_jsonl(self) -> list[dict[str, Any]]:
        """Read all valid JSON lines from the JSONL file."""
        entries: list[dict[str, Any]] = []
        try:
            with open(self._jsonl_path) as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        entries.append(obj)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Invalid JSON at %s:%d: %s",
                            self._jsonl_path.name,
                            line_num,
                            e,
                        )
        except Exception as e:
            logger.warning("Failed to read JSONL %s: %s", self._jsonl_path, e)

        return entries

    def _reconstruct_history(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Reconstruct conversation history from JSONL entries.

        Handles:
        - User messages: type="user"
        - Assistant messages: type="assistant"
        - Tool calls: type="tool_use" (accumulated into assistant message)
        - Tool results: type="tool_result" (accumulated into user message)
        - Metadata entries: type="orchestrator_meta", "voice_interrupted" (ignored)
        """
        history: list[dict[str, Any]] = []

        # State for accumulating multi-block messages
        pending_assistant_blocks: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        for entry in entries:
            msg_type = entry.get("type")

            # Skip metadata entries
            if msg_type in ("orchestrator_meta", "voice_interrupted"):
                continue

            # User message
            if msg_type == "user":
                self._flush_pending_assistant(history, pending_assistant_blocks)
                self._flush_pending_tool_results(history, pending_tool_results)

                msg = entry.get("message", {})
                content = msg.get("content", "")
                if content:  # Skip empty user messages
                    history.append({"role": "user", "content": content})

            # Assistant message
            elif msg_type == "assistant":
                self._flush_pending_tool_results(history, pending_tool_results)

                msg = entry.get("message", {})
                content = msg.get("content", "")

                # Convert to content block format if needed
                if isinstance(content, str) and content:
                    pending_assistant_blocks.append({"type": "text", "text": content})
                elif isinstance(content, list):
                    pending_assistant_blocks.extend(content)

                # Flush if there are no pending tool calls (pure text response)
                if not self._has_tool_calls(pending_assistant_blocks):
                    self._flush_pending_assistant(history, pending_assistant_blocks)

            # Tool use (part of assistant message)
            elif msg_type == "tool_use":
                tool_block = {
                    "type": "tool_use",
                    "id": entry.get("tool_call_id", ""),
                    "name": entry.get("tool_name", ""),
                    "input": entry.get("tool_input", {}),
                }
                pending_assistant_blocks.append(tool_block)

            # Tool result (will be part of user message)
            elif msg_type == "tool_result":
                self._flush_pending_assistant(history, pending_assistant_blocks)

                result_block = {
                    "type": "tool_result",
                    "tool_use_id": entry.get("tool_call_id", ""),
                    "content": entry.get("output", ""),
                }
                if entry.get("is_error"):
                    result_block["is_error"] = True

                pending_tool_results.append(result_block)

        # Flush any remaining pending content
        self._flush_pending_assistant(history, pending_assistant_blocks)
        self._flush_pending_tool_results(history, pending_tool_results)

        return history

    @staticmethod
    def _has_tool_calls(blocks: list[dict[str, Any]]) -> bool:
        """Check if content blocks contain any tool_use blocks."""
        return any(b.get("type") == "tool_use" for b in blocks if isinstance(b, dict))

    @staticmethod
    def _flush_pending_assistant(
        history: list[dict[str, Any]],
        pending_blocks: list[dict[str, Any]],
    ) -> None:
        """Flush pending assistant content blocks to history."""
        if pending_blocks:
            history.append({"role": "assistant", "content": pending_blocks.copy()})
            pending_blocks.clear()

    @staticmethod
    def _flush_pending_tool_results(
        history: list[dict[str, Any]],
        pending_results: list[dict[str, Any]],
    ) -> None:
        """Flush pending tool results as a user message."""
        if pending_results:
            history.append({"role": "user", "content": pending_results.copy()})
            pending_results.clear()


class HistoryWriter:
    """Writes conversation events to a JSONL file.

    Each event is written as a single JSON line with a timestamp.
    """

    def __init__(self, jsonl_path: Path) -> None:
        self._jsonl_path = jsonl_path

    def append(self, data: dict[str, Any]) -> None:
        """Append a single event to the JSONL file."""
        try:
            with open(self._jsonl_path, "a") as f:
                f.write(json.dumps(data) + "\n")
        except Exception as e:
            logger.warning("Failed to write to JSONL %s: %s", self._jsonl_path, e)
