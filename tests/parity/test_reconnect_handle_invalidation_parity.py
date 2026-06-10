"""Parity contract: Gemini Live stale-handle one-shot recovery.

If Gemini Live force-closes mid-session with WS 1008 "BidiGenerateContent
session expired" AND we hold a resumption handle, the relay must:

1. drop the poisoned handle so the rebuild ships an empty
   ``sessionResumption`` block (forces a fresh session),
2. set ``_stale_handle_recovery_used = True`` so a second 1008 in a row
   does NOT trigger another fresh-setup attempt (prevents infinite
   loops on a genuinely-broken upstream),
3. count against ``max_reconnects`` like any other recoverable error.

The Increment C refactor MUST NOT break the one-shot guard. The
production-quality test uses the real :class:`GeminiAIStudioBackend` to
exercise its own ``is_recoverable_error`` mutation.

See plan §C ("STALE_HANDLE policy: max_attempts=1, reset_handle=True")
and §11.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any
from unittest.mock import patch

import pytest

from orchestrator.providers.gemini_voice import GeminiAIStudioBackend


class _StaleFakeWS:
    def __init__(self, frames, close_error=None, clean_close=False):
        self._frames: deque[str] = deque(frames)
        self._close_error = close_error
        self._clean_close = clean_close
        self.sent: list[str] = []
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._frames:
            return self._frames.popleft()
        if self._close_error is not None:
            err = self._close_error
            self._close_error = None
            raise err
        raise StopAsyncIteration

    async def recv(self):
        if self._frames:
            return self._frames.popleft()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    async def send(self, payload):
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = True
        self.close_code = code
        self.close_reason = reason


def test_stale_handle_recovery_is_one_shot():
    """First 1008 close with a held handle: ``is_recoverable_error``
    returns True AND clears ``_resumption_handle``. Second call returns
    False even with the same conditions — one-shot guard.

    This is a pure-state test of the provider (no relay), pinning the
    contract the relay depends on.
    """
    backend = GeminiAIStudioBackend()
    # Simulate a held handle without any goAway received.
    backend._resumption_handle = "POISONED-HANDLE"
    backend._goaway_received = False
    backend._stale_handle_recovery_used = False

    exc = ConnectionError(
        "received 1008 (policy violation) BidiGenerateContent session expired; "
        "then sent 1008 (policy violation) BidiGenerateContent session expired"
    )

    # First call: handle is dropped, one-shot fired.
    assert backend.is_recoverable_error(exc) is True
    assert backend._resumption_handle is None
    assert backend._stale_handle_recovery_used is True

    # Second call (same exception class): no handle, no recovery left.
    assert backend.is_recoverable_error(exc) is False


def test_stale_handle_recovery_resets_on_fresh_setup_complete():
    """``setupComplete`` on a NEW session must reset
    ``_stale_handle_recovery_used`` to False, so a future stale-handle
    event in a long-running conversation (across multiple goAways) can
    still recover. The provider does this in ``on_inbound_event``.
    """
    backend = GeminiAIStudioBackend()
    backend._stale_handle_recovery_used = True

    # ``on_inbound_event`` resets the flag on setupComplete.
    backend.on_inbound_event({"setupComplete": {}})

    assert backend._stale_handle_recovery_used is False, (
        "setupComplete must reset _stale_handle_recovery_used so the "
        "one-shot recovery is per-session, not per-relay-lifetime"
    )


def test_goaway_path_keeps_handle_alive():
    """Sanity: the goAway + handle case must remain recoverable (the
    standard long-call path). The stale-handle gate must NOT short-circuit
    the goAway path.
    """
    backend = GeminiAIStudioBackend()
    backend._resumption_handle = "LIVE-HANDLE"
    backend._goaway_received = True
    backend._stale_handle_recovery_used = False

    exc = ConnectionError("clean 1000 after goAway")

    assert backend.is_recoverable_error(exc) is True
    # Handle preserved — production rebuild uses it.
    assert backend._resumption_handle == "LIVE-HANDLE"
    # One-shot guard NOT triggered (this was a goAway, not a stale handle).
    assert backend._stale_handle_recovery_used is False
