"""OpenAI Chat Completions provider with multimodal (audio) support.

Supports both GPT-4o text and audio input. Audio is passed directly to the model
as base64-encoded content blocks, leveraging GPT-4o's native multimodal capabilities.

This provider translates between Anthropic-style tool definitions and OpenAI's
function calling format, enabling seamless model switching within the orchestrator.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

import openai

from orchestrator.types import (
    ErrorEvent,
    OrchestratorEvent,
    TextComplete,
    TextDelta,
    ToolUseStart,
    TurnComplete,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants and Configuration
# ---------------------------------------------------------------------------

class OpenAIModel(str, Enum):
    """Supported OpenAI models for text/multimodal chat."""

    GPT_4O = "gpt-4o"
    # `gpt-4o-audio-preview` was renamed by OpenAI to the `gpt-audio` family and
    # now 404s on newer accounts; `gpt-audio` / `gpt-audio-mini` are the current
    # audio-input chat models (verified 2026-07-21).
    GPT_AUDIO = "gpt-audio"  # Audio input (current)
    GPT_AUDIO_MINI = "gpt-audio-mini"  # Audio input (current, cheaper)
    GPT_4O_AUDIO = "gpt-4o-audio-preview"  # Legacy audio (may 404 — kept for old accounts)
    GPT_4O_MINI = "gpt-4o-mini"
    GPT_4O_MINI_AUDIO = "gpt-4o-mini-audio-preview"  # Legacy audio
    GPT_4_TURBO = "gpt-4-turbo"
    GPT_4 = "gpt-4"

    @property
    def supports_audio(self) -> bool:
        """Whether this model supports audio input."""
        return self in (
            OpenAIModel.GPT_AUDIO,
            OpenAIModel.GPT_AUDIO_MINI,
            OpenAIModel.GPT_4O_AUDIO,
            OpenAIModel.GPT_4O_MINI_AUDIO,
        )

    @property
    def supports_vision(self) -> bool:
        """Whether this model supports image input."""
        return self in (
            OpenAIModel.GPT_4O,
            OpenAIModel.GPT_AUDIO,
            OpenAIModel.GPT_AUDIO_MINI,
            OpenAIModel.GPT_4O_MINI,
            OpenAIModel.GPT_4_TURBO,
        )


# Default model for the provider — current audio-capable model for multimodal.
DEFAULT_MODEL = OpenAIModel.GPT_AUDIO
DEFAULT_MAX_TOKENS = 8192


# ---------------------------------------------------------------------------
# Audio Content Handling
# ---------------------------------------------------------------------------

# Formats supported by OpenAI's audio input
OPENAI_AUDIO_FORMATS = {"wav", "mp3"}


@dataclass(frozen=True, slots=True)
class AudioContent:
    """Represents audio content for multimodal messages.

    Note: OpenAI only supports 'wav' and 'mp3' formats. Use
    orchestrator.audio_utils.convert_audio_to_wav() to convert
    other formats (webm, ogg) before creating AudioContent.
    """

    data: str  # Base64-encoded audio data
    format: str  # "wav" or "mp3" only

    def __post_init__(self) -> None:
        """Validate that format is supported by OpenAI."""
        if self.format not in OPENAI_AUDIO_FORMATS:
            raise ValueError(
                f"Unsupported audio format '{self.format}'. "
                f"OpenAI only accepts: {', '.join(sorted(OPENAI_AUDIO_FORMATS))}"
            )

    @classmethod
    def from_bytes(cls, audio_bytes: bytes, audio_format: str) -> "AudioContent":
        """Create AudioContent from raw bytes."""
        return cls(
            data=base64.b64encode(audio_bytes).decode("utf-8"),
            format=audio_format.lower().lstrip("."),
        )

    def to_openai_content_block(self) -> dict[str, Any]:
        """Convert to OpenAI input_audio content block format."""
        return {
            "type": "input_audio",
            "input_audio": {
                "data": self.data,
                "format": self.format,
            },
        }


# ---------------------------------------------------------------------------
# Tool Format Conversion
# ---------------------------------------------------------------------------

def anthropic_to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Anthropic tool definitions to OpenAI function format.

    Anthropic format:
        {
            "name": "tool_name",
            "description": "...",
            "input_schema": { JSON Schema }
        }

    OpenAI format:
        {
            "type": "function",
            "function": {
                "name": "tool_name",
                "description": "...",
                "parameters": { JSON Schema }
            }
        }
    """
    openai_tools = []
    for tool in tools:
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return openai_tools


def _anthropic_image_to_openai(block: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an Anthropic ``image`` block to an OpenAI ``image_url`` block.

    Returns None if the source shape isn't understood (caller falls back to a
    text placeholder rather than emitting an invalid block).
    """
    source = block.get("source", {})
    if isinstance(source, dict) and source.get("type") == "base64":
        media = source.get("media_type", "image/png")
        data = source.get("data", "")
        if data:
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{media};base64,{data}"},
            }
    # Some paths already carry an OpenAI-shaped image_url — pass through.
    if isinstance(block.get("image_url"), dict):
        return {"type": "image_url", "image_url": block["image_url"]}
    return None


def _coerce_block_to_openai(block: Any) -> dict[str, Any]:
    """Coerce ANY content block into an OpenAI-valid block (text/image_url).

    This is the defensive backstop that prevents one malformed block — e.g. a
    tool that returned a raw Anthropic ``image`` block, or any unrecognized
    type — from 400-ing the whole request ("Content blocks are expected to be
    either text or image_url type") and wedging the session so every following
    turn re-sends the bad block and fails again. Anything we can't map to a
    real block becomes a text rendering so the model still sees *something*.
    """
    if isinstance(block, dict):
        btype = block.get("type")
        if btype == "text":
            return {"type": "text", "text": str(block.get("text", ""))}
        if btype in ("image_url", "input_audio"):
            return block  # already OpenAI-valid
        if btype == "image":
            converted = _anthropic_image_to_openai(block)
            if converted is not None:
                return converted
            return {"type": "text", "text": "[image omitted]"}
        # Unknown block type — render as text so it can't break the request.
        return {"type": "text", "text": _stringify_block(block)}
    # Non-dict block (str, etc.)
    return {"type": "text", "text": str(block)}


def _stringify_block(block: dict[str, Any]) -> str:
    """Best-effort text rendering of an unknown content block."""
    for key in ("text", "content", "output"):
        val = block.get(key)
        if isinstance(val, str) and val:
            return val
    try:
        return json.dumps(block, ensure_ascii=False)[:4000]
    except (TypeError, ValueError):
        return str(block)[:4000]


def _coerce_content_to_openai(content: Any) -> str | list[dict[str, Any]]:
    """Normalize a message/tool ``content`` field to an OpenAI-valid shape.

    OpenAI accepts either a plain string or a list of text/image_url (/input_audio)
    blocks. A string passes through unchanged; a list has every block coerced;
    anything else is stringified. Guarantees the return can never trip the
    "Content blocks are expected to be either text or image_url type" 400.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        coerced = [_coerce_block_to_openai(b) for b in content]
        # Collapse a pure-text list to a plain string — smaller and avoids
        # edge cases with empty lists.
        if coerced and all(b.get("type") == "text" for b in coerced):
            return "\n".join(b.get("text", "") for b in coerced)
        return coerced or ""
    if content is None:
        return ""
    return str(content)


def _sanitize_all_content(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Coerce every message's ``content`` to an OpenAI-valid shape.

    The last line of defense before the request goes out. Preserves each
    message's other keys (role, tool_calls, tool_call_id). An assistant
    message that legitimately has ``content: None`` alongside ``tool_calls``
    is left as-is (OpenAI requires null content there); only genuine content
    payloads are coerced.
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if "content" not in m:
            out.append(m)
            continue
        # Assistant tool-call carrier with null content + tool_calls is valid.
        if (
            m.get("role") == "assistant"
            and m.get("content") is None
            and m.get("tool_calls")
        ):
            out.append(m)
            continue
        m = dict(m)
        m["content"] = _coerce_content_to_openai(m.get("content"))
        out.append(m)
    return out


def convert_messages_for_openai(
    messages: list[dict[str, Any]],
    system: str,
) -> list[dict[str, Any]]:
    """Convert Anthropic-style messages to OpenAI format.

    Handles:
    - System message (injected as first message with role="system")
    - User/assistant messages with text and tool results
    - Tool use blocks → function call format
    - Tool results → tool role messages (converted from user messages with tool_result blocks)

    Anthropic puts tool results in user messages as:
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}

    OpenAI requires tool results as separate messages:
        {"role": "tool", "tool_call_id": "...", "content": "..."}
    """
    openai_messages: list[dict[str, Any]] = []

    # Add system message first
    if system:
        openai_messages.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "user":
            converted = _convert_user_message(msg)
            # _convert_user_message returns None if the message only contained tool_results
            # (which are returned as a list of tool messages)
            if isinstance(converted, list):
                # List of tool result messages
                openai_messages.extend(converted)
            elif converted is not None:
                openai_messages.append(converted)
        elif role == "assistant":
            openai_messages.extend(_convert_assistant_message(msg))

    # Sanitize in two directions so the OpenAI ordering invariant holds:
    #   1. assistant.tool_calls must be immediately followed by a matching
    #      `tool` message for every id (else 400: "tool_calls did not have
    #      response messages").
    #   2. every `tool` message must be immediately preceded by an assistant
    #      message whose tool_calls include its id (else 400: "messages with
    #      role 'tool' must be a response to a preceding message with
    #      'tool_calls'").
    # Both can be violated when a JSONL was reconstructed with an unrelated
    # message interposed between a tool_use and its tool_result — e.g. a
    # spurious [voice] transcription landing mid tool-execution splits the
    # assistant tool_use from its results, orphaning the results.
    sanitized = _sanitize_orphaned_tool_calls(openai_messages)
    ordered = _drop_orphaned_tool_messages(sanitized)
    # Final defense-in-depth pass: guarantee every message's content is an
    # OpenAI-valid shape (string, or list of text/image_url/input_audio
    # blocks). Any block a per-message converter missed — a raw image, an
    # unknown type from a future tool — is coerced to text here rather than
    # 400-ing the whole request and wedging the session. Runs regardless of
    # which path produced the message, so it can never be bypassed.
    return _sanitize_all_content(ordered)


def _drop_orphaned_tool_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop `tool` messages not answering an immediately-preceding tool_call.

    OpenAI requires every ``role: "tool"`` message to be part of a contiguous
    run that directly follows an assistant message carrying the matching
    ``tool_calls``. A tool message whose id is not in the nearest preceding
    assistant ``tool_calls`` (or that follows a non-tool_calls message) is
    orphaned and triggers a 400.

    Rather than discard the result payload outright, an orphaned tool result
    is re-attached to the conversation as a plain user message so the model
    still sees the information, just not in tool-response position.
    """
    result: list[dict[str, Any]] = []
    # Set of tool_call ids answerable at the current position — populated by
    # the most recent assistant message with tool_calls, consumed as the
    # contiguous run of tool messages after it is walked.
    open_ids: set[str] = set()

    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            open_ids = {tc["id"] for tc in msg.get("tool_calls", []) if tc.get("id")}
            result.append(msg)
        elif role == "tool":
            tid = msg.get("tool_call_id", "")
            if tid in open_ids:
                # Valid response; consume the id so a duplicate can't reuse it.
                open_ids.discard(tid)
                result.append(msg)
            else:
                # Orphaned — preserve the payload as a user message instead of
                # emitting an invalid `tool` message.
                content = msg.get("content", "")
                if content:
                    result.append({
                        "role": "user",
                        "content": f"[tool result {tid}]\n{content}",
                    })
        else:
            # Any non-tool message closes the answerable window.
            open_ids = set()
            result.append(msg)

    return result


def _sanitize_orphaned_tool_calls(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove tool_calls from assistant messages not followed by tool results.

    OpenAI requires that every tool_call_id in an assistant message has a
    corresponding tool role message immediately after. If any are missing
    (e.g., due to interrupted sessions), strip the tool_calls entirely to
    avoid a 400 error.
    """
    result: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Collect expected tool_call_ids
            expected_ids = {tc["id"] for tc in msg["tool_calls"]}
            # Collect tool result ids from immediately following tool messages
            j = i + 1
            found_ids: set[str] = set()
            while j < len(messages) and messages[j].get("role") == "tool":
                found_ids.add(messages[j].get("tool_call_id", ""))
                j += 1
            # Check if all tool_calls have responses
            if expected_ids != found_ids:
                # Strip tool_calls; keep text content if any
                sanitized = {k: v for k, v in msg.items() if k != "tool_calls"}
                # If content is None and no tool_calls, skip entirely
                if sanitized.get("content") is not None:
                    result.append(sanitized)
                # Also skip the orphaned tool result messages (if partial)
                i = j
                continue
        result.append(msg)
        i += 1
    return result


def _convert_user_message(msg: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Convert a user message to OpenAI format.

    Returns:
        - A single user message dict for normal messages
        - A list of tool result messages if the message only contains tool_results
        - None if the message should be skipped
    """
    content = msg.get("content", "")

    # Simple text message
    if isinstance(content, str):
        return {"role": "user", "content": content}

    # Multimodal content (list of blocks)
    if isinstance(content, list):
        openai_content: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in content:
            block_type = block.get("type", "")

            if block_type == "text":
                openai_content.append({
                    "type": "text",
                    "text": block.get("text", ""),
                })
            elif block_type == "input_audio":
                # Already in OpenAI format (from our audio handling)
                openai_content.append(block)
            elif block_type == "image":
                # Anthropic image format → OpenAI (base64 or already-OpenAI
                # image_url). A shape we can't map becomes a text placeholder
                # instead of silently vanishing.
                openai_content.append(_coerce_block_to_openai(block))
            elif block_type == "tool_result":
                # Tool results in user messages (Anthropic style)
                # Convert to OpenAI tool role messages. Coerce the content so a
                # tool that returned structured/image blocks can't emit a raw
                # non-text/image_url block (the 400 that wedged the session).
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _coerce_content_to_openai(block.get("content", "")),
                })
            else:
                # Unknown block type in a user message — render as text rather
                # than dropping it silently (or, worse, passing it through raw).
                openai_content.append(_coerce_block_to_openai(block))

        # If message ONLY contains tool results, return them as tool messages
        if tool_results and not openai_content:
            return tool_results

        # If message has both content and tool results, this is unusual but handle it
        # Return user content and tool results will need special handling
        if tool_results and openai_content:
            # This shouldn't normally happen, but if it does, prioritize tool results
            # since OpenAI requires them immediately after tool_calls
            return tool_results

        if openai_content:
            return {"role": "user", "content": openai_content}

        return None

    return {"role": "user", "content": str(content)}


def _convert_assistant_message(msg: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert an assistant message to OpenAI format.

    May return multiple messages if there are tool calls followed by tool results.
    """
    content = msg.get("content", "")
    result: list[dict[str, Any]] = []

    # Simple text response
    if isinstance(content, str):
        if content:
            result.append({"role": "assistant", "content": content})
        return result

    # Complex content with potential tool calls
    if isinstance(content, list):
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []

        for block in content:
            block_type = block.get("type", "")

            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif block_type == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _coerce_content_to_openai(block.get("content", "")),
                })

        # Build assistant message
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if text_parts:
            assistant_msg["content"] = "\n".join(text_parts)
        else:
            assistant_msg["content"] = None

        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls

        if assistant_msg.get("content") or assistant_msg.get("tool_calls"):
            result.append(assistant_msg)

        # Add tool results as separate messages
        result.extend(tool_results)

    return result


# ---------------------------------------------------------------------------
# OpenAI Text Provider
# ---------------------------------------------------------------------------

class OpenAITextProvider:
    """Model provider using OpenAI Chat Completions API with streaming.

    Supports:
    - Text input/output with GPT-4 family models
    - Audio input with GPT-4o (multimodal)
    - Tool/function calling
    - Streaming responses

    Reads OPENAI_API_KEY from environment.
    """

    def __init__(
        self,
        model: str | OpenAIModel = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        if isinstance(model, OpenAIModel):
            self._model = model.value
            self._model_enum = model
        else:
            self._model = model
            try:
                self._model_enum = OpenAIModel(model)
            except ValueError:
                self._model_enum = None

        self._max_tokens = max_tokens
        self._client = openai.AsyncOpenAI()

    @property
    def model(self) -> str:
        """The model identifier string."""
        return self._model

    @property
    def supports_audio(self) -> bool:
        """Whether the current model supports audio input."""
        return self._model_enum is not None and self._model_enum.supports_audio

    async def create_message(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
    ) -> AsyncIterator[OrchestratorEvent]:
        """Stream a model response, yielding orchestrator events.

        Handles text streaming, tool call accumulation, and usage tracking.
        Automatically converts Anthropic-style messages and tools to OpenAI format.
        """
        # Convert to OpenAI format
        openai_messages = convert_messages_for_openai(messages, system)
        openai_tools = anthropic_to_openai_tools(tools) if tools else None

        # Build request kwargs
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if openai_tools:
            kwargs["tools"] = openai_tools
            kwargs["tool_choice"] = "auto"

        # State for accumulating streamed content
        current_text = ""
        tool_calls: dict[int, dict[str, Any]] = {}  # index → {id, name, arguments}
        input_tokens = 0
        output_tokens = 0

        try:
            stream = await self._client.chat.completions.create(**kwargs)

            async for chunk in stream:
                # Handle usage in final chunk
                if chunk.usage:
                    input_tokens = chunk.usage.prompt_tokens
                    output_tokens = chunk.usage.completion_tokens

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                if delta is None:
                    continue

                # Text content
                if delta.content:
                    current_text += delta.content
                    yield TextDelta(text=delta.content)

                # Tool calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index

                        if idx not in tool_calls:
                            tool_calls[idx] = {
                                "id": tc.id or "",
                                "name": tc.function.name if tc.function else "",
                                "arguments": "",
                            }

                        if tc.id:
                            tool_calls[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls[idx]["arguments"] += tc.function.arguments

                # Check for finish reason
                if choice.finish_reason:
                    # Emit text complete if we accumulated text
                    if current_text:
                        yield TextComplete(text=current_text)

                    # Emit tool use events for any accumulated tool calls
                    for tc_data in tool_calls.values():
                        try:
                            tool_input = json.loads(tc_data["arguments"]) if tc_data["arguments"] else {}
                        except json.JSONDecodeError:
                            tool_input = {}

                        yield ToolUseStart(
                            tool_call_id=tc_data["id"],
                            tool_name=tc_data["name"],
                            tool_input=tool_input,
                        )

            yield TurnComplete(input_tokens=input_tokens, output_tokens=output_tokens)

        except openai.APIError as e:
            logger.exception("OpenAI API error")
            yield ErrorEvent(error="api_error", detail=str(e))
        except Exception as e:
            logger.exception("Unexpected error in OpenAI provider")
            yield ErrorEvent(error="provider_error", detail=str(e))


# ---------------------------------------------------------------------------
# Helper Functions for Audio Messages
# ---------------------------------------------------------------------------

def create_audio_message(
    audio_data: bytes | str,
    audio_format: str,
    text_prompt: str | None = None,
) -> dict[str, Any]:
    """Create a user message with audio content for GPT-4o.

    Args:
        audio_data: Raw audio bytes or base64-encoded string
        audio_format: Audio format ("wav" or "mp3" only - use
            orchestrator.audio_utils.convert_audio_to_wav() for other formats)
        text_prompt: Optional text to accompany the audio

    Returns:
        Message dict ready for the conversation history

    Raises:
        ValueError: If audio_format is not supported by OpenAI
    """
    # Handle base64 string or bytes
    if isinstance(audio_data, bytes):
        audio = AudioContent.from_bytes(audio_data, audio_format)
    else:
        audio = AudioContent(data=audio_data, format=audio_format)

    content: list[dict[str, Any]] = [audio.to_openai_content_block()]

    if text_prompt:
        content.insert(0, {"type": "text", "text": text_prompt})

    return {
        "role": "user",
        "content": content,
    }
