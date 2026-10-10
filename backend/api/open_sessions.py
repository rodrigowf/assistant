"""The server's open set, persisted so it survives restarts (spec 12 OPEN-1).

The pool is the single source of truth for which conversations are open — the
Archie (orchestrator) conversation and every agent session — on every device.
A deploy or crash restarts the backend; without this file the pool would come
back empty and every device would lose its open conversations.  The pool writes
the set here whenever it changes and restores it at startup: restored sessions
are listed as open and spawn on first use (``SessionPool.ensure_live``).

Machine-local (``state/`` at the repo root, gitignored): each backend has its own
pool, so this file must not live in the synced ``context/``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OpenSet:
    """``agents``: ``(local_id, sdk_session_id or None)`` in open order.
    ``orchestrator``: ``(local_id, jsonl_id)`` of the Archie conversation, if open."""

    agents: tuple[tuple[str, str | None], ...] = ()
    orchestrator: tuple[str, str] | None = None


class OpenSessionsStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._saved: OpenSet | None = None

    def load(self) -> OpenSet:
        try:
            raw = json.loads(self._path.read_text())
            agents = tuple((a["local_id"], a.get("sdk_session_id")) for a in raw.get("agents", []))
            orch = raw.get("orchestrator")
            open_set = OpenSet(agents, (orch["local_id"], orch["jsonl_id"]) if orch else None)
        except FileNotFoundError:
            open_set = OpenSet()
        except (OSError, ValueError, KeyError, TypeError):
            logger.exception("Unreadable %s; starting with nothing open", self._path)
            open_set = OpenSet()
        self._saved = open_set
        return open_set

    def save(self, open_set: OpenSet) -> None:
        """Write atomically; a no-op when nothing changed (called on every turn end)."""
        if open_set == self._saved:
            return
        body = {
            "agents": [{"local_id": lid, "sdk_session_id": sdk} for lid, sdk in open_set.agents],
            "orchestrator": (
                {"local_id": open_set.orchestrator[0], "jsonl_id": open_set.orchestrator[1]}
                if open_set.orchestrator else None
            ),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, indent=1))
            os.replace(tmp, self._path)
            self._saved = open_set
        except OSError:
            logger.exception("Failed to persist the open set to %s", self._path)
