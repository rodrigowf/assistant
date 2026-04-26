"""Token budgeting helpers for the orchestrator's voice system prompt.

Uses a char-based heuristic (~3.5 chars/token) to avoid a heavy native
tokenizer dep. This over-estimates slightly vs. tiktoken's o200k_base, which
is the safe direction when we're fitting into a context window.
"""

from __future__ import annotations

from typing import Any

# gpt-realtime context window
MODEL_CONTEXT_TOKENS = 32_000

# Total budget for the voice system prompt — leaves ~14k for live turns + output.
MAX_VOICE_PROMPT_TOKENS = 18_000

# Within the prompt, the history section (summary + recent verbatim) gets ~12k.
HISTORY_SECTION_TOKENS = 12_000

# Of that, ~8k is kept verbatim (newest messages), ~4k is the summary budget.
RECENT_VERBATIM_TOKENS = 8_000
SUMMARY_MAX_TOKENS = 4_000

# Tool results in the verbatim history are clipped to this many chars plus a
# short "re-read to get full content" hint, so huge tool outputs don't eat the
# budget.
TOOL_RESULT_TRUNCATE_CHARS = 700
TOOL_RESULT_TRUNCATE_SUFFIX = (
    "... [tool result truncated — re-read the file or re-run the tool if you "
    "need the full content]"
)


def estimate_tokens(text: str) -> int:
    """Conservative char-based token estimate (~3.5 chars/token).

    Over-estimates vs. tiktoken for mixed EN/PT which is the safe direction.
    """
    if not text:
        return 0
    return max(int(len(text) / 3.5), 1)


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate tokens for a single Anthropic-format message."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return estimate_tokens(content) + 4  # role overhead
    total = 4
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype == "text":
                total += estimate_tokens(block.get("text", ""))
            elif btype == "tool_use":
                import json as _json
                try:
                    input_str = _json.dumps(block.get("input", {}))
                except Exception:
                    input_str = str(block.get("input", ""))
                total += estimate_tokens(block.get("name", "")) + estimate_tokens(input_str) + 8
            elif btype == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                total += estimate_tokens(str(result_content)) + 4
    return total


def truncate_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a deep-ish copy of messages with oversized tool_result contents clipped.

    Does not mutate the originals. Tool inputs/calls are left intact — only the
    potentially-large result payloads are clipped.
    """
    out: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            out.append(msg)
            continue

        new_blocks: list[dict[str, Any]] = []
        changed = False
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_blocks.append(block)
                continue

            result = block.get("content", "")
            # Normalize to string for length check
            if isinstance(result, list):
                text = " ".join(
                    b.get("text", "") for b in result
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            else:
                text = str(result)

            if len(text) > TOOL_RESULT_TRUNCATE_CHARS:
                clipped = text[:TOOL_RESULT_TRUNCATE_CHARS] + TOOL_RESULT_TRUNCATE_SUFFIX
                new_block = dict(block)
                new_block["content"] = clipped
                new_blocks.append(new_block)
                changed = True
            else:
                new_blocks.append(block)

        if changed:
            new_msg = dict(msg)
            new_msg["content"] = new_blocks
            out.append(new_msg)
        else:
            out.append(msg)
    return out


def split_by_token_budget(
    messages: list[dict[str, Any]],
    budget_tokens: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split messages into (to_summarize, recent_verbatim) by walking from newest.

    Accumulates tokens from the newest message backward until the budget is
    exhausted. Everything older goes to the summarize bucket.

    Returns (older_messages_to_summarize, recent_messages_verbatim).
    """
    if not messages:
        return [], []

    total = 0
    cutoff = len(messages)  # index of first kept-verbatim message
    for i in range(len(messages) - 1, -1, -1):
        msg_tokens = estimate_message_tokens(messages[i])
        if total + msg_tokens > budget_tokens and cutoff < len(messages):
            # Already have at least one verbatim message — stop here.
            break
        total += msg_tokens
        cutoff = i

    return messages[:cutoff], messages[cutoff:]


def scale_summary_max_tokens(prefix_message_count: int, prefix_tokens: int) -> int:
    """Pick a max_tokens for the summarizer based on how much we're compressing.

    Shorter prefixes → shorter summaries. Longer prefixes → longer summaries,
    bounded by SUMMARY_MAX_TOKENS.
    """
    if prefix_message_count == 0:
        return 0
    # ~1 summary token per 10 prefix tokens, floor 256, ceil SUMMARY_MAX_TOKENS
    scaled = max(256, prefix_tokens // 10)
    return min(scaled, SUMMARY_MAX_TOKENS)
