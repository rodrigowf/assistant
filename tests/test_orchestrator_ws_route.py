"""Regression tests for the orchestrator WS route handler.

Focused on lifecycle edge cases — paths through which the receive loop
can exit *without* going through either except handler. Those paths
must still leave the local variables the finally block reads in a
consistent state.

Live failure that motivated this file (2026-06-04):
- The silence-timeout ``break`` introduced in the heartbeat refactor
  exited the loop normally, skipping both ``except WebSocketDisconnect``
  and ``except Exception`` where ``was_voice`` was assigned.
- The finally block read ``was_voice`` → ``UnboundLocalError``.
- Every Android reconnect after a clean voice stop hit the silence
  path and crashed, producing a tight crash spam loop that froze the
  app.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import orjson
import pytest
from starlette.testclient import TestClient


@pytest.fixture
def fast_silence_timeout():
    """Patch the silence timeout to something a test can actually wait
    out without sleeping the suite."""
    with patch("api.routes.orchestrator._WS_CLIENT_SILENCE_TIMEOUT_S", 0.5):
        yield


@pytest.fixture
def fast_heartbeat():
    """Disable the server heartbeat for tests that care about *no*
    inbound traffic — otherwise the heartbeat keeps the loop alive."""
    with patch("api.routes.orchestrator._WS_HEARTBEAT_INTERVAL_S", 9999.0):
        yield


@pytest.fixture
def app_client():
    from api.app import create_app
    app = create_app()
    with TestClient(app) as client:
        yield client


class TestSilenceTimeout:
    def test_silent_ws_closes_without_crash(
        self, app_client, fast_silence_timeout, fast_heartbeat,
    ):
        """The receive loop must exit cleanly (not with
        UnboundLocalError) when the client sends nothing within the
        silence window. Reproduces the live crash where a fresh WS
        that never sent any message — e.g. an Android reconnect after
        a clean voice stop — froze the app with crash spam.
        """
        # If the route crashes inside its finally block, TestClient
        # raises. Just opening and waiting through the timeout window
        # is enough to prove the path doesn't crash.
        with app_client.websocket_connect("/api/orchestrator/chat") as ws:
            # Wait a touch longer than the patched timeout. The server
            # should close us cleanly with code 1011.
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect) as exc_info:
                # Block until the server closes us. With the patched
                # 0.5s silence timeout, this should return in under
                # a second. If the route handler crashed in finally
                # (the bug we're guarding against), TestClient surfaces
                # the exception instead of a clean WebSocketDisconnect.
                ws.receive_text()
            # Code 1011 is what our silence-timeout close path sends.
            assert exc_info.value.code == 1011

    def test_silent_ws_after_clean_voice_stop(
        self, app_client, fast_silence_timeout, fast_heartbeat,
    ):
        """Same as above but exercises the path where there's NO voice
        session attached. ``was_voice`` was undefined in the finally
        block for this case in the live crash. Even with no session
        the handler must exit cleanly.
        """
        with app_client.websocket_connect("/api/orchestrator/chat") as ws:
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
