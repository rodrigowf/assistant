"""Typed voice-provider error surface.

Until Increment A (2026-06-09), the relay surfaced every upstream close
behind a single opaque ``voice_relay_failed`` event the frontend
rendered as a generic red banner. Users couldn't tell a billing-cap
close apart from a transient network blip — and the relay burned
reconnect attempts on the former, amplifying user-visible disruption.

This module introduces a typed error envelope. Each provider
(:class:`~orchestrator.providers.gemini_voice_base.GeminiVoiceProviderBase`,
:class:`~orchestrator.providers.openai_voice.OpenAIVoiceProvider`,
:class:`~orchestrator.providers.qwen_voice.QwenVoiceProvider`)
implements ``classify_close_reason`` to map its provider-specific wire
patterns into shared categories. The relay emits a ``voice_error``
event carrying the typed payload alongside the legacy ``error`` event
(for back-compat) and short-circuits reconnect when the classifier
flags ``recoverable=False``.

Design contract (also enforced by
``tests/parity/test_voice_error_recoverable_parity.py``):

  ``classify_close_reason`` is READ-ONLY and ADVISORY. The legacy
  ``is_recoverable_error`` remains the source of truth for reconnect
  gating in :class:`~orchestrator.voice_relay.VoiceRelay`. Wherever the
  new classifier emits a ``VoiceError`` row, its ``recoverable`` flag
  MUST match what the legacy gate would return for the same exception.

This separation is the smallest change that ships typed UX while
preserving Gemini's stateful stale-handle one-shot recovery (the
legacy gate mutates ``_resumption_handle`` and
``_stale_handle_recovery_used`` exactly once per fresh-handle attempt).
A future increment can unify the two methods; Increment A does not.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class VoiceErrorCategory(str, enum.Enum):
    """Categories surfaced to the UI. String values are wire bytes — the
    frontend and Android parser switch on these strings, so renaming a
    member silently breaks the banner. New categories may be appended.
    """

    QUOTA_EXCEEDED = "quota_exceeded"      # billing cap, monthly limit
    RATE_LIMIT = "rate_limit"              # RPM/RPS throttle (recoverable)
    AUTH = "auth"                          # expired / revoked / wrong project
    MODEL_UNAVAILABLE = "model_unavailable"  # regional, discontinued, wrong tier
    CONTEXT_FULL = "context_full"          # token budget exhausted
    NETWORK = "network"                    # transport-layer (recoverable)
    PROVIDER_INTERNAL = "provider_internal"  # 1011 with no semantic match
    UNKNOWN = "unknown"                    # fallback


@dataclass(frozen=True)
class VoiceError:
    """Typed upstream-provider error.

    Built by :meth:`BaseVoiceProvider.classify_close_reason` and
    emitted by :class:`VoiceRelay` as a ``voice_error`` event payload.
    Frozen so the relay can hand the same instance to multiple
    subscribers without worrying about mutation.
    """

    category: VoiceErrorCategory
    message: str
    """Human-readable summary. Must contain the canonical wire phrase
    the close exposes (e.g. ``"exceeded its monthly spending cap"``) so
    existing log searches keep working.
    """
    recoverable: bool
    """If False, the relay sets ``_max_reconnects = 0`` immediately —
    further closes propagate without retry. MUST match what the
    provider's :meth:`is_recoverable_error` would return for the same
    exception (parity contract).
    """
    recovery_hint: str | None
    """One-sentence next step the user can act on. None for purely
    transient categories (NETWORK / RATE_LIMIT, where the UI shows
    auto-retry chrome).
    """
    provider_doc_url: str | None
    """Link the UI surfaces as a click-through (billing page, API key
    settings, etc.). May be None.
    """
    raw_close_code: int | None
    """WebSocket close code (1006, 1007, 1008, 1011, ...) or HTTP
    status (401, 429, ...) when known. None when the underlying
    exception didn't carry one.
    """
    raw_close_reason: str | None
    """Verbatim close-reason or response-body fragment. Preserved so
    diagnostic playbooks can still grep the same strings post-refactor.
    """
    provider: str
    """The originating provider's :attr:`provider_name`
    (``"google"`` / ``"openai"`` / ``"qwen"``).
    """

    def to_event(self) -> dict:
        """Build the ``voice_error`` WebSocket envelope.

        This shape is the wire contract between backend and clients
        (frontend ``useVoiceOrchestrator.ts``, Android
        ``WebSocketManager.kt``). Don't reorder or rename fields without
        coordinating both client sides.
        """
        return {
            "type": "voice_error",
            "error": {
                "category": self.category.value,
                "message": self.message,
                "recoverable": self.recoverable,
                "recovery_hint": self.recovery_hint,
                "provider_doc_url": self.provider_doc_url,
                "raw_close_code": self.raw_close_code,
                "raw_close_reason": self.raw_close_reason,
                "provider": self.provider,
            },
        }
