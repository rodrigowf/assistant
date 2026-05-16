"""HarnessRegistry — single source of truth for "what is a session provider?"

Each session harness (Claude Code, Qwen Code, future CLIs) registers a
``HarnessSpec`` here.  Every code path that used to branch on the literal
strings ``"claude"`` / ``"qwen"`` now goes through this registry instead,
so adding a third harness is a single-file addition: write the spec, call
:func:`register_harness`, done.

The registry intentionally does NOT import the concrete session manager
classes at module load.  Importing ``ClaudeSessionManager`` pulls in
``claude-agent-sdk`` (a hard runtime dep for Claude that's pointless for
a Qwen-only install) and vice versa, so each spec carries a lazy
``session_class_loader`` callable that the pool invokes only when a
session of that provider is actually requested.

The PEP 562 lazy mechanism in :mod:`manager.__init__` makes the *names*
``ClaudeSessionManager`` / ``QwenSessionManager`` resolvable without
forcing the import; the registry composes on top of that to also keep
the *dispatch site* (``api.pool._session_manager_for``) ignorant of
which providers exist.

Two concerns this registry deliberately does NOT own:

* The JSONL adapter contract (``ProviderAdapter``) lives in
  :mod:`manager.protocol`.  Each ``HarnessSpec`` carries a reference to
  the adapter so callers that already have a spec can reach the adapter
  without a second lookup, but the adapter type and the
  ``register_provider()`` side-effect are unchanged.
* Voice providers (OpenAI Realtime, Qwen-Omni, …) are a separate axis
  with their own registry in :mod:`orchestrator.providers.voice_registry`.
  Don't merge them — a harness ↔ voice mapping is many-to-many.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base_session import BaseSessionManager
    from .protocol import ProviderAdapter


# Callable types for the lazy-loaded pieces.  Spec authors return concrete
# classes/functions; the registry resolves them on demand.
SessionClassLoader = Callable[[], type["BaseSessionManager"]]
AdapterLoader = Callable[[], "ProviderAdapter"]
KillHelperLoader = Callable[[], Callable[[int], bool]]
JsonlPathResolver = Callable[[str], list[Path]]
# Yields (session_id, jsonl_path) for every JSONL this harness has on disk
# for the given project_dir.  Lets SessionStore enumerate sessions that
# live outside the project's context/ folder (notably Gemini, which writes
# under ~/.gemini/tmp/<label>/chats/ — globally, so the discoverer must
# filter by project to keep the listing project-scoped).
SessionDiscoverer = Callable[[str], "Iterable[tuple[str, Path]]"]


@dataclass(frozen=True)
class HarnessSpec:
    """Everything a session harness contributes to the dispatch layer.

    Frozen so registry entries can be shared safely across threads and
    accidentally-mutating a spec from a test fixture is a hard error.

    Fields
    ------
    name
        Canonical provider id used everywhere in config (``"claude"``,
        ``"qwen"``).  Lower-case, no spaces — this is the key the user
        types in ``assistant_config.json``.
    label
        Short human-readable label for the UI dropdown.
    description
        One-line description for the UI dropdown.
    session_class_loader
        Returns the ``BaseSessionManager`` subclass for this harness.
        Invoked lazily so importing the registry doesn't drag in
        claude-agent-sdk on a Qwen-only host.
    adapter_loader
        Returns the ``ProviderAdapter`` for parsing this harness's
        JSONL.  Invoked at ``ensure_all_registered()`` time.
    comm_prefix
        The kernel-comm prefix of this harness's subprocess (e.g.
        ``"claude"`` for the bundled Claude CLI, ``"node"`` for Qwen's
        Node-based shim).  Used by the orphan reaper for ``/proc/<pid>/comm``
        sanity checks before SIGKILL.
    kill_helper_loader
        Returns ``kill_<harness>_subprocess(pid) -> bool``.  Lazy for the
        same reason as ``session_class_loader``.
    ssh_control_path_prefix
        First component of ``/tmp/<prefix>-ssh-<host>-%r`` for SSH
        multiplexing.  Must be unique per harness so two harnesses talking
        to the same host don't share a ControlMaster socket lifetime.
    jsonl_path_resolver
        Given a session id, returns the *candidate* paths where this
        harness's JSONL would live (most return one, but a harness with
        both legacy and current layouts returns both — the caller
        is_file()-checks each).
    session_discoverer
        Optional.  Yields ``(session_id, jsonl_path)`` for every JSONL
        this harness has on disk.  Used by :class:`SessionStore` to
        enumerate sessions stored *outside* the project's ``context/``
        folder (Gemini writes under ``~/.gemini/tmp/<label>/chats/``;
        Claude and Qwen both live inside ``context/`` and don't need
        this hook — the store scans those directories directly).
    requirements_file
        Pip requirements file specific to this harness (used by
        ``install.sh``'s registry-driven loop).  None for harnesses that
        ship as pure dependencies of the manager package.
    npm_package
        Global npm package name for the CLI binary (``install.sh`` hint
        only — we don't auto-install).  None for non-npm harnesses.
    cli_binary
        Name of the CLI executable on ``PATH`` (e.g. ``"claude"``,
        ``"qwen"``).  Used by ``install-prerequisites.sh`` checks.
    env_keys
        Environment variable keys this harness needs in ``context/.env``.
        ``install.sh`` warns the user if any are missing.
    """

    name: str
    label: str
    description: str
    session_class_loader: SessionClassLoader
    adapter_loader: AdapterLoader
    comm_prefix: str
    kill_helper_loader: KillHelperLoader
    ssh_control_path_prefix: str
    jsonl_path_resolver: JsonlPathResolver
    session_discoverer: SessionDiscoverer | None = None
    requirements_file: str | None = None
    npm_package: str | None = None
    cli_binary: str | None = None
    env_keys: tuple[str, ...] = field(default_factory=tuple)


class HarnessRegistry:
    """In-process registry of session-harness specs.

    Population is side-effecting: each adapter module
    (:mod:`manager.claude.adapter`, :mod:`manager.qwen.adapter`, …) calls
    :func:`register_harness` at import time.  :func:`ensure_all_registered`
    triggers those imports so callers don't need to know which adapter
    modules exist.
    """

    def __init__(self) -> None:
        self._specs: dict[str, HarnessSpec] = {}

    def register(self, spec: HarnessSpec) -> None:
        """Register a harness spec (idempotent — last wins).

        Last-wins so a test can replace a spec for fixture purposes without
        having to clear the registry first; in production the imports run
        exactly once.
        """
        self._specs[spec.name] = spec

    def get(self, name: str) -> HarnessSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> HarnessSpec:
        """Like ``get()`` but raises ``ValueError`` for unknown names.

        Most dispatch sites should use this — silently falling back when
        the user typed a typo just hides the bug.
        """
        spec = self._specs.get(name)
        if spec is None:
            raise ValueError(
                f"Unknown session harness {name!r}; "
                f"registered: {sorted(self._specs)}",
            )
        return spec

    def names(self) -> tuple[str, ...]:
        """Return registered harness names, in registration order."""
        return tuple(self._specs)

    def all(self) -> dict[str, HarnessSpec]:
        return dict(self._specs)


_registry = HarnessRegistry()


def get_registry() -> HarnessRegistry:
    return _registry


def register_harness(spec: HarnessSpec) -> None:
    _registry.register(spec)


def registered_provider_names() -> tuple[str, ...]:
    """Convenience: just the names, for places that don't need full specs."""
    return _registry.names()


def ensure_all_registered() -> None:
    """Import every adapter module so its registration side-effect runs.

    Safe to call repeatedly.  Adapter modules also register a
    :class:`~manager.protocol.ProviderAdapter` instance for JSONL detection
    — that contract is intentionally kept in :mod:`manager.protocol` so
    code that only needs JSONL parsing doesn't need to know about
    harnesses.

    Mirror of :func:`manager.protocol.ensure_all_registered` and kept in
    sync with it; importing an adapter module triggers both registrations
    because each adapter module calls both ``register_harness`` and
    ``register_provider`` at module scope.
    """
    import importlib
    for mod in _ADAPTER_MODULES:
        importlib.import_module(mod)


# Modules that, on import, register both a HarnessSpec and a ProviderAdapter.
# Listed here (not derived from _registry) because we don't know what's
# registered until after the imports run — chicken-and-egg.  Adding a new
# harness lands by writing the adapter module and adding it here.
_ADAPTER_MODULES: tuple[str, ...] = (
    "manager.claude.adapter",
    "manager.qwen.adapter",
    "manager.gemini.adapter",
)
