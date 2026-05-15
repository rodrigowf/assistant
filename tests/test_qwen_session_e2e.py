"""End-to-end integration tests for QwenSessionManager.

These tests spawn the *real* ``qwen`` CLI subprocess (no mocks).  They're
skipped when:

- The ``qwen`` binary isn't on ``$PATH`` (or at ``QWEN_CLI_PATH``).
- ``QWEN_E2E=1`` isn't set in the environment.

The second gate exists because the tests make real network requests to
DashScope (Qwen's API) and consume tokens — we don't want them running
in unattended CI by default.  Local runs:

    QWEN_E2E=1 .venv/bin/python -m pytest tests/test_qwen_session_e2e.py -v
"""

from __future__ import annotations

import os
import shutil

import pytest

from manager.config import ManagerConfig
from manager.qwen.session import QwenSessionManager
from manager.types import TextDelta, TurnComplete


def _qwen_available() -> bool:
    return shutil.which(os.environ.get("QWEN_CLI_PATH", "qwen")) is not None


pytestmark = [
    pytest.mark.skipif(
        os.environ.get("QWEN_E2E") != "1",
        reason="Set QWEN_E2E=1 to run real-Qwen integration tests (costs tokens).",
    ),
    pytest.mark.skipif(
        not _qwen_available(),
        reason="`qwen` CLI not found on PATH; install via `npm install -g @qwen-code/qwen-code`.",
    ),
]


@pytest.mark.asyncio
async def test_single_turn_completes(tmp_path):
    """A simple one-turn interaction should produce text and a TurnComplete."""
    sm = QwenSessionManager(config=ManagerConfig(project_dir=str(tmp_path)))
    await sm.start()
    try:
        text_received: list[str] = []
        turn_complete: TurnComplete | None = None
        async for event in sm.send(
            "Reply with exactly the word PINEAPPLE and nothing else.",
        ):
            if isinstance(event, TextDelta):
                text_received.append(event.text)
            elif isinstance(event, TurnComplete):
                turn_complete = event

        assert turn_complete is not None
        full_text = "".join(text_received)
        assert "PINEAPPLE" in full_text.upper(), \
            f"Expected 'PINEAPPLE' in output, got: {full_text!r}"
        # Provider session id must be captured for resume to work next turn.
        assert sm.sdk_session_id is not None
        assert sm.turns >= 1
    finally:
        await sm.stop()


@pytest.mark.asyncio
async def test_multi_turn_resume(tmp_path):
    """A second turn on the same session must remember the first turn.

    Qwen is one-shot per subprocess; multi-turn works by passing
    ``--resume <session_id>`` on the second invocation.  This test verifies
    that the session_id captured from the init event of turn 1 actually
    reaches turn 2's argv.
    """
    sm = QwenSessionManager(config=ManagerConfig(project_dir=str(tmp_path)))
    await sm.start()
    try:
        # Turn 1: plant a memorable token.
        async for event in sm.send(
            "Remember the secret word RAVIOLI. Acknowledge with 'ok'.",
        ):
            if isinstance(event, TurnComplete):
                break
        first_session = sm.sdk_session_id
        assert first_session is not None

        # Turn 2: retrieve it.  Session id should stay stable across turns.
        text_received: list[str] = []
        async for event in sm.send(
            "What was the secret word? Reply with just the word.",
        ):
            if isinstance(event, TextDelta):
                text_received.append(event.text)
            elif isinstance(event, TurnComplete):
                break

        assert sm.sdk_session_id == first_session, \
            "session_id changed between turns — resume isn't keeping state"
        full_text = "".join(text_received).upper()
        assert "RAVIOLI" in full_text, \
            f"Qwen lost context across turns. Got: {full_text!r}"
    finally:
        await sm.stop()
