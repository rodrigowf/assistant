"""Tests for api/connections.py — ConnectionManager."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from api.connections import ConnectionManager


@pytest.fixture
def cm():
    return ConnectionManager()


def _mock_sm(stop_error=False):
    sm = MagicMock()
    sm.stop = AsyncMock(side_effect=RuntimeError("boom") if stop_error else None)
    return sm


class TestConnectionManager:
    def test_starts_empty(self, cm):
        assert cm.active_count == 0

    def test_connect_and_get(self, cm):
        ws, sm = MagicMock(), _mock_sm()
        cm.connect("s1", ws, sm)
        assert cm.is_active("s1")
        assert cm.get("s1") == (ws, sm)
        assert cm.active_count == 1

    def test_get_missing(self, cm):
        assert cm.get("nope") is None
        assert not cm.is_active("nope")

    async def test_disconnect_calls_stop(self, cm):
        ws, sm = MagicMock(), _mock_sm()
        cm.connect("s1", ws, sm)
        await cm.disconnect("s1")
        sm.stop.assert_awaited_once()
        assert not cm.is_active("s1")
        assert cm.active_count == 0

    async def test_disconnect_missing_is_noop(self, cm):
        await cm.disconnect("nope")  # should not raise

    async def test_disconnect_swallows_stop_error(self, cm):
        ws, sm = MagicMock(), _mock_sm(stop_error=True)
        cm.connect("s1", ws, sm)
        await cm.disconnect("s1")  # should not raise
        assert not cm.is_active("s1")

    def test_multiple_sessions(self, cm):
        for i in range(3):
            cm.connect(f"s{i}", MagicMock(), _mock_sm())
        assert cm.active_count == 3
        assert cm.is_active("s0")
        assert cm.is_active("s2")
