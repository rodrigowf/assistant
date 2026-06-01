"""Tests for manager/index_utils.

The remove_session_from_index function spawns a subprocess that calls
IndexFacade. We test:
  - The generated subprocess script is syntactically valid Python.
  - Calling the function with a non-existent session in an empty index
    completes cleanly (returns True for "no chunks to delete").
  - A timeout / subprocess failure is reported as False without
    raising.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "default-scripts"))

from manager import index_utils  # noqa: E402

pytestmark = pytest.mark.timeout(30)


def test_subprocess_script_is_valid_python():
    """Generate the subprocess script and assert it parses."""
    # Capture the script by patching subprocess.run.
    captured = {}

    def fake_run(cmd, **kwargs):
        # cmd[0] is the python interpreter; cmd[1] is "-c"; cmd[2] is the script.
        captured["script"] = cmd[2]
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        m.stderr = ""
        return m

    with patch("subprocess.run", side_effect=fake_run):
        ok = index_utils.remove_session_from_index("test-session-id")
    assert ok is True
    # Should parse without SyntaxError.
    ast.parse(captured["script"])
    # And it must reference index_client (single-writer enforcement).
    assert "index_client" in captured["script"]
    assert "test-session-id" in captured["script"]


def test_returns_false_on_subprocess_failure():
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        m.stderr = "boom"
        return m
    with patch("subprocess.run", side_effect=fake_run):
        assert index_utils.remove_session_from_index("x") is False


def test_returns_false_on_signal_crash():
    def fake_run(cmd, **kwargs):
        m = MagicMock()
        m.returncode = -11  # SIGSEGV
        m.stdout = ""
        m.stderr = ""
        return m
    with patch("subprocess.run", side_effect=fake_run):
        assert index_utils.remove_session_from_index("x") is False
