"""Voice-event JSONL persistence.

Increment G (plan §G) extracts the persistence logic that previously
lived inline in ``OrchestratorSession.process_voice_event`` into one
class. The session keeps only the parts that genuinely belong to it:

- Tool execution (touches the registry + the session's context dict).
- Provider command formatting (``provider.format_tool_result``).
- Provider lifecycle (``inject_event`` queues the event for the agent
  loop).

Everything else — the per-provider event dispatch, the staged
transcript accumulation, the audio-recorder segment markers, the
``[voice] X`` / ``[voice, recording: ...]`` content formatting, the
``is_injecting`` gating — moves here. The persister is fed the same
raw events the session received, plus the provider name, and emits
the same JSONL writes via the supplied :class:`HistoryWriter`.

Behavior preservation (plan §0.1)
---------------------------------

Every JSONL ``writer.append`` call from the legacy
``process_voice_event`` is reproduced here byte-for-byte (modulo the
``timestamp`` field, which is wall-clock and not part of the parity
contract). The parity tests in
``tests/parity/test_voice_persister_parity.py`` pin this against HEAD;
the Inc G commit keeps them green.

State
-----

Two pieces of session-scoped state move here:

- ``_pending_user_transcript: str | None`` — buffers Gemini Live's
  token-level ``inputTranscription`` deltas until the user turn
  ends (first ``outputTranscription`` from the model OR a fall-back
  ``turnComplete`` for audio-only turns).
- ``_pending_assistant_transcript: str | None`` — buffers the
  assistant's ``audio_transcript`` (OpenAI/Qwen) or ``outputTranscription``
  (Gemini) until the turn ends with ``status="completed"``. A
  cancelled turn drops the staged transcript without persisting —
  the load-bearing behavior that prevents history pollution from
  fragments like "Yeah, I think" after barge-in.

The session reads these via the persister's public ``stage_*`` /
``has_pending_*`` API when it needs them (today only used by the
parity tests + the session's tear-down clear path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from orchestrator.audio_recorder import AudioRecorder
    from orchestrator.persistence import HistoryWriter


class VoicePersister:
    """Owns the voice-event → JSONL pipeline for one session.

    Constructed once per :class:`OrchestratorSession` and lives the
    lifetime of the session (text-mode sessions construct one anyway —
    it's cheap; voice mode populates it via ``process_voice_event``).

    Collaborators:

    * ``writer`` — :class:`HistoryWriter` for JSONL writes.
    * ``local_id`` — used in the ``[voice, recording: <id> ...]`` text
      that surfaces audio-segment references in chat content.
    * ``get_audio_recorder()`` — callable returning the current
      :class:`AudioRecorder` (or None). Indirect because the recorder
      can be swapped/cleared mid-session (e.g., on reconnect).
    * ``is_injecting()`` — callable returning the session's
      ``is_injecting`` flag. Indirect for the same reason.

    The two collaborators are callables (not attributes) so the
    persister doesn't shadow the session's runtime state.
    """

    def __init__(
        self,
        *,
        writer: "HistoryWriter",
        local_id: str,
        get_audio_recorder: Callable[[], "AudioRecorder | None"],
        is_injecting: Callable[[], bool],
    ) -> None:
        self._writer = writer
        self._local_id = local_id
        self._get_audio_recorder = get_audio_recorder
        self._is_injecting = is_injecting

        # Staged transcript buffers — see module docstring.
        self._pending_user_transcript: str | None = None
        self._pending_assistant_transcript: str | None = None

    # ------------------------------------------------------------------
    # Stage / clear API (read by the session on teardown)
    # ------------------------------------------------------------------

    @property
    def pending_user_transcript(self) -> str | None:
        return self._pending_user_transcript

    @property
    def pending_assistant_transcript(self) -> str | None:
        return self._pending_assistant_transcript

    def clear_pending(self) -> None:
        """Drop both staged buffers without persisting. Called on
        reconnect teardown so a half-finished turn can't bleed across
        sessions.
        """
        self._pending_user_transcript = None
        self._pending_assistant_transcript = None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle_event(self, event: dict[str, Any], provider_name: str) -> None:
        """Dispatch a raw provider event to the right per-provider
        handler.

        ``provider_name`` mirrors ``BaseVoiceProvider.provider_name`` —
        currently ``"google"`` (Gemini Live), ``"openai"``, ``"qwen"``,
        or a test fake. The dispatch matches the legacy
        ``process_voice_event`` exactly:

        - Gemini Live: identified by ``not event_type and provider_name
          == "google"``. Uses the camelCase-keyed shape.
        - OpenAI / Qwen: anything else with a top-level ``type``.
        """
        event_type = event.get("type", "")
        if not event_type and provider_name == "google":
            self._handle_gemini_event(event)
        else:
            self._handle_top_level_type_event(event_type, event)

    # ------------------------------------------------------------------
    # Gemini Live persistence
    # ------------------------------------------------------------------

    def _handle_gemini_event(self, event: dict[str, Any]) -> None:
        """Persist a Gemini Live event. Identified by the camelCase
        top-level keys (``serverContent``, ``toolCall``, ...). The
        order of probes here matches the legacy code's ``if/elif`` chain
        EXACTLY — see ``tests/parity/test_voice_persister_parity.py``.
        """
        sc = event.get("serverContent") or {}
        input_t = sc.get("inputTranscription") if isinstance(sc, dict) else None
        output_t = sc.get("outputTranscription") if isinstance(sc, dict) else None

        # User speech transcript — accumulate; flushed on first output
        # delta or turnComplete failsafe.
        if isinstance(input_t, dict) and not self._is_injecting():
            delta = input_t.get("text", "")
            if delta:
                staged = self._pending_user_transcript or ""
                self._pending_user_transcript = staged + delta

        # Assistant transcript delta — accumulate; persist on
        # turnComplete. The arrival of an output delta also flushes any
        # buffered user transcript (the user's turn ended).
        if isinstance(output_t, dict):
            staged_user = self._pending_user_transcript
            if staged_user and not self._is_injecting():
                self._flush_pending_user_transcript(staged_user)
            delta = output_t.get("text", "")
            if delta:
                staged = self._pending_assistant_transcript or ""
                self._pending_assistant_transcript = staged + delta

        # Turn complete — persist staged transcripts.
        if isinstance(sc, dict) and sc.get("turnComplete"):
            # Failsafe flush of user transcript: covers turns where the
            # model produced no text output (audio-only modality) so the
            # outputTranscription branch above never fired.
            staged_user = self._pending_user_transcript
            if staged_user and not self._is_injecting():
                self._flush_pending_user_transcript(staged_user)
            self._flush_assistant_transcript_if_staged()

        # Interrupted — mark in JSONL like OpenAI's speech_started.
        if isinstance(sc, dict) and sc.get("interrupted") and not self._is_injecting():
            self._writer.append({
                "type": "voice_interrupted",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    # ------------------------------------------------------------------
    # OpenAI / Qwen persistence (top-level type dispatch)
    # ------------------------------------------------------------------

    def _handle_top_level_type_event(
        self, event_type: str, event: dict[str, Any]
    ) -> None:
        # User speech transcript — Whisper completion.
        if (
            event_type == "conversation.item.input_audio_transcription.completed"
            and not self._is_injecting()
        ):
            transcript = event.get("transcript", "")
            if transcript:
                self._persist_user_turn(transcript)

        # User typed text inside a voice session.
        elif event_type == "conversation.item.created":
            item = event.get("item", {})
            if item.get("role") == "user":
                for c in item.get("content", []):
                    if c.get("type") == "input_text" and c.get("text"):
                        self._writer.append({
                            "type": "user",
                            "message": {"role": "user", "content": c["text"]},
                            "source": "voice_transcription",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        })
                        break

        # Assistant transcript complete — STAGE only.
        elif event_type in (
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        ):
            transcript = event.get("transcript", "")
            if transcript:
                self._pending_assistant_transcript = transcript

        # Turn complete — flush staged ONLY when status="completed".
        # Cancelled turns drop the fragment without persisting.
        elif event_type == "response.done":
            response = event.get("response", {})
            status = response.get("status", "completed")
            staged = self._pending_assistant_transcript
            if staged and status == "completed":
                self._flush_assistant_transcript_if_staged()
            else:
                # Cancelled / failed — drop the staged fragment.
                self._pending_assistant_transcript = None

        # Barge-in — record JSONL marker (suppressed while injecting).
        elif event_type == "input_audio_buffer.speech_started":
            if not self._is_injecting():
                self._writer.append({
                    "type": "voice_interrupted",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

    # ------------------------------------------------------------------
    # Tool persistence (called by OrchestratorSession after registry
    # execution — the persister doesn't run tools, but it owns the
    # tool_use / tool_result JSONL shape).
    # ------------------------------------------------------------------

    def persist_tool_use_and_result(
        self,
        *,
        call_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        output: str,
    ) -> None:
        """Append the canonical tool_use + tool_result JSONL pair.

        Matches the legacy ``self._writer.append({...})`` pair in
        ``process_voice_event`` (both the OpenAI/Qwen path and the
        Gemini path used the same shape).
        """
        now = datetime.now(timezone.utc).isoformat()
        self._writer.append({
            "type": "tool_use",
            "tool_call_id": call_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "source": "voice",
            "timestamp": now,
        })
        self._writer.append({
            "type": "tool_result",
            "tool_call_id": call_id,
            "output": output,
            "is_error": False,
            "source": "voice",
            "timestamp": now,
        })

    # ------------------------------------------------------------------
    # Internal helpers (private — exposed only via tests through the
    # public ``handle_event`` and ``persist_*`` API).
    # ------------------------------------------------------------------

    def _flush_pending_user_transcript(self, transcript: str) -> None:
        """Write the buffered user transcript as a single JSONL entry.

        Used by the Gemini Live event path where ``inputTranscription``
        arrives as token-level deltas across many events.
        """
        self._persist_user_turn(transcript)
        self._pending_user_transcript = None

    def _persist_user_turn(self, transcript: str) -> None:
        """Write one user JSONL entry, attaching an audio-segment
        reference if the recorder is active.
        """
        segment = None
        recorder = self._get_audio_recorder()
        if recorder is not None and recorder.is_recording:
            segment = recorder.mark_user_turn_end(transcript)
        if segment:
            content = (
                f"[voice, recording: {self._local_id} "
                f"{segment['start_ms']}-{segment['end_ms']}ms] {transcript}"
            )
        else:
            content = f"[voice] {transcript}"
        entry: dict[str, Any] = {
            "type": "user",
            "message": {"role": "user", "content": content},
            "source": "voice_transcription",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if segment:
            entry["audio_segment"] = segment
        self._writer.append(entry)

    def _flush_assistant_transcript_if_staged(self) -> None:
        """If there's a staged assistant transcript, persist it (with
        optional audio segment reference) and clear the buffer.
        """
        staged = self._pending_assistant_transcript
        if not staged:
            return
        segment = None
        recorder = self._get_audio_recorder()
        if recorder is not None and recorder.is_recording:
            segment = recorder.mark_assistant_turn_end(staged)
        if segment:
            content = (
                f"[voice, recording: {self._local_id} "
                f"{segment['start_ms']}-{segment['end_ms']}ms] {staged}"
            )
        else:
            content = staged
        entry: dict[str, Any] = {
            "type": "assistant",
            "message": {"role": "assistant", "content": content},
            "source": "voice_response",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if segment:
            entry["audio_segment"] = segment
        self._writer.append(entry)
        self._pending_assistant_transcript = None
