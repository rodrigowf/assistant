"""Reconnect reason taxonomy + policy table for :class:`VoiceRelay`.

Before Increment C the relay's ``_try_reconnect`` mixed two reconnect
flavors behind a boolean kwarg (``is_goaway``). The classes diverged on:

- whether the attempt counts against ``max_reconnects``,
- whether to surface ``reconnect_warning`` + ``reconnecting`` status
  events to the user,
- whether to reset the provider's resumption handle,
- whether to reset local VAD state.

Tucking those four axes into a named :class:`ReconnectReason` + frozen
:class:`ReconnectPolicy` does three things:

1. It exposes a third reason — ``STALE_HANDLE`` — that the provider gate
   previously handled by mutating its own private state inside
   ``is_recoverable_error``. The relay can now ask explicitly without
   reading the provider's mind.
2. It documents the contract once, in a single table, instead of
   re-checking the boolean at every branch in the reconnect path.
3. It lets tests pin behavior on the policy itself (see
   :mod:`tests.test_voice_reconnect_lock_and_queue`).

The values in :data:`POLICIES` are the pre-Increment-C HEAD behavior
translated into the new shape. Per plan §0.1 the refactor preserves
behavior verbatim; the parity tests in
``tests/parity/test_reconnect_*_parity.py`` enforce that.

Plan reference: §C "Parameterised reconnect with single lock + held
outbound queue".
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


# Soft cap on the held-outbound queue. The queue parks frontend control
# events while the relay is mid-reconnect and the handshake gate is
# closed; after the new ``setupComplete`` arrives, the queue flushes in
# order. 64 is a wide safety margin (a normal call queues 0–3 frames
# across a sub-second reconnect) chosen to bound memory if a reconnect
# stalls in production.
HELD_OUTBOUND_CAP = 64


class ReconnectReason(str, enum.Enum):
    """Why the relay is opening a new upstream WS.

    Members carry their string label so logs / slogs render the reason
    without needing a lookup. Used as a dict key in :data:`POLICIES` and
    as a tag in :meth:`VoiceRelay._try_reconnect` slogs.
    """

    # Provider sent a ``goAway`` lifecycle frame (Gemini Live's ~10-min
    # session limit warning). Protocol-driven and intended — NOT an
    # error. Uncapped: the user can keep talking as long as they want.
    PROVIDER_GOAWAY = "provider_goaway"

    # Transient transport close that ``is_recoverable_error`` flagged as
    # worth retrying (DashScope's mid-session 1007 InvalidParameter,
    # idle timeouts, etc.). Capped by ``max_reconnects`` to prevent a
    # storm on a permanently broken upstream.
    RECOVERABLE_ERROR = "recoverable_error"

    # One-shot recovery from a 1008 "session expired" close where the
    # provider's saved resumption handle was poisoned. Drops the handle
    # so the rebuild ships a fresh session — at most once per relay
    # lifetime (the provider's own ``_stale_handle_recovery_used`` flag
    # guards against tighter loops).
    STALE_HANDLE = "stale_handle"


@dataclass(frozen=True)
class ReconnectPolicy:
    """Behavior knobs for a single :class:`ReconnectReason`.

    Read by :meth:`VoiceRelay._try_reconnect` to decide:

    - whether to admit the attempt (``max_attempts``),
    - whether to drop the provider's resumption handle before rebuild
      (``reset_handle``),
    - whether to reset local VAD state (``reset_vad_state``),
    - whether to emit ``reconnect_warning`` + ``reconnecting`` status
      events to the user (``surface_to_user``).
    """

    reason: ReconnectReason
    # 0 means uncapped (PROVIDER_GOAWAY). Otherwise: the maximum number
    # of attempts this reason can consume from ``_reconnect_count``
    # before the relay surfaces the error and gives up.
    max_attempts: int
    reset_handle: bool
    reset_vad_state: bool
    surface_to_user: bool


# HEAD-behavior table. Per plan §0.1 these MUST match what the relay
# does at d675187 — the parity tests in tests/parity/test_reconnect_*
# enforce that. Do not edit these without a matching test update.
POLICIES: dict[ReconnectReason, ReconnectPolicy] = {
    ReconnectReason.PROVIDER_GOAWAY: ReconnectPolicy(
        reason=ReconnectReason.PROVIDER_GOAWAY,
        max_attempts=0,           # uncapped
        reset_handle=False,       # preserve handle; rebuild uses it
        reset_vad_state=True,     # Silero recurrent state stale after seam
        surface_to_user=True,     # reconnect_warning + reconnecting status
    ),
    ReconnectReason.RECOVERABLE_ERROR: ReconnectPolicy(
        reason=ReconnectReason.RECOVERABLE_ERROR,
        max_attempts=2,           # HEAD default for max_reconnects
        reset_handle=False,
        reset_vad_state=True,
        surface_to_user=True,
    ),
    ReconnectReason.STALE_HANDLE: ReconnectPolicy(
        reason=ReconnectReason.STALE_HANDLE,
        max_attempts=1,           # one-shot
        reset_handle=True,        # drop poisoned handle before rebuild
        reset_vad_state=True,
        surface_to_user=False,    # silent recovery
    ),
}


def policy_for(reason: ReconnectReason) -> ReconnectPolicy:
    """Look up the policy for a reason. Tiny helper for call-site
    readability; equivalent to ``POLICIES[reason]``.
    """
    return POLICIES[reason]
