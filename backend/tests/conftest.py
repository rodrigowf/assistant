import sys
from pathlib import Path

# Make shared/scripts/ importable (search.py, index_client.py, … live here).
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "shared" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import os
import tempfile

import pytest

# The backend's machine-local state (the persisted open set) goes to a temp dir,
# never the real ``state/`` a running backend on this machine restores from.
os.environ["ARCHIE_STATE_DIR"] = tempfile.mkdtemp(prefix="archie-test-state-")

# ``context/scripts/run.sh`` exports the real provider credentials from
# context/.env.  Harness catalog loaders (and a few other code paths) call
# live APIs when a key is present, so a test that forgot to mock them would
# quietly spend quota or depend on the network.  Tests that need a key set
# their own with monkeypatch.setenv (which runs after this fixture).
_LIVE_CREDENTIALS = (
    "DASHSCOPE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_live_credentials(monkeypatch):
    for key in _LIVE_CREDENTIALS:
        monkeypatch.delenv(key, raising=False)
    from manager.harness_catalog import clear_catalog_cache
    clear_catalog_cache()
    yield
    clear_catalog_cache()


# ---------------------------------------------------------------------------
# The real context/ is off limits.
#
# Tests used to leave fixture sessions in the private context repo: the
# orchestrator WS tests wrote ``context/orch-1.jsonl`` (a real
# OrchestratorSession persists its meta line to ``get_sessions_dir()``), and
# ``SessionStore.delete_session`` moved fixture JSONLs (``indexed-session``,
# ``qwen-sess-1``) into the real ``context/trash/`` on every run.  Those show
# up as pending changes in the context repo and sync to the other machine.
#
# 1. Every OrchestratorSession created during the run persists into a
#    sandbox (tests that patch ``orchestrator.session.get_sessions_dir``
#    themselves still win — their patch is applied on top).
# 2. At the end of the run, any new entry in context/, context/chats/ or
#    context/trash/ whose name a real session could not have (UUID ids and
#    Gemini ``session-*`` files are left alone: a live backend may be writing
#    those while the suite runs) fails the run.
# ---------------------------------------------------------------------------

import re as _re

_REAL_CONTEXT = Path(__file__).resolve().parents[2] / "context"
_LIVE_NAME = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|^session-\d{4}-\d{2}-\d{2}T"
)


def _real_context_entries() -> set[str]:
    out: set[str] = set()
    for sub in ("", "chats", "trash"):
        d = _REAL_CONTEXT / sub if sub else _REAL_CONTEXT
        try:
            names = [p.name for p in d.iterdir()]
        except OSError:
            continue
        out.update(f"{sub}/{n}" if sub else n for n in names)
    return out


def _is_test_leak(entry: str) -> bool:
    name = entry.rsplit("/", 1)[-1]
    return not name.startswith(".") and not _LIVE_NAME.match(name)


@pytest.fixture(scope="session", autouse=True)
def _real_context_is_off_limits(tmp_path_factory):
    before = _real_context_entries()
    sandbox = tmp_path_factory.mktemp("orchestrator-sessions")
    mp = pytest.MonkeyPatch()
    import orchestrator.session as _orch_session
    mp.setattr(_orch_session, "get_sessions_dir", lambda: sandbox)
    try:
        yield sandbox
    finally:
        mp.undo()
    leaked = sorted(e for e in _real_context_entries() - before if _is_test_leak(e))
    if leaked:
        pytest.fail(
            "tests wrote into the real context/ (use tmp_path): " + ", ".join(leaked),
            pytrace=False,
        )


@pytest.fixture
def context_leak_filter():
    """The predicate the guard above uses (exposed for its own tests)."""
    return _is_test_leak
