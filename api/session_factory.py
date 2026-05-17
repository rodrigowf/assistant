"""Shared builder for agent ``ManagerConfig`` instances.

Both the UI's "+" button ([api.routes.chat]) and the orchestrator's
``open_agent_session`` tool need a fully-resolved ``ManagerConfig`` that
honours the user's saved global configuration: the active working
directory (local path or SSH target), the chosen session-harness, the
per-provider harness model, the chrome flag, and the enabled MCPs.

Before this module existed the orchestrator built its config from a
process-start snapshot of ``ManagerConfig.load()`` (stuck in
``app.state.config``) and ignored ``assistant_config.json`` entirely, so
orchestrator-spawned sessions silently ran locally with default
provider/model/MCPs even when the UI was pointed at a remote SSH host.
Centralising the build here means the two surfaces stay in lockstep:
adding a new global-config knob automatically flows into both.

The single entry point is :func:`build_session_config`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from manager.config import ManagerConfig

logger = logging.getLogger(__name__)


def build_session_config(
    *,
    resume_sdk_id: str | None = None,
    mcp_override: list[str] | None = None,
) -> tuple[ManagerConfig, dict[str, dict] | None, dict[str, Any]]:
    """Resolve a ``ManagerConfig`` + MCP servers dict for a new agent session.

    Re-reads ``.manager.json`` and ``assistant_config.json`` on every call —
    never caches.  This is what makes "edit config in the UI, then call
    open_agent_session" do the right thing.

    Args:
        resume_sdk_id: When resuming an existing JSONL, the SDK session
            id used as the lookup key for the per-session config file.
            For fresh sessions pass ``None``.
        mcp_override: Optional list of MCP server names that the caller
            wants for this specific session.  Layered on top of the
            global ``enabled_mcps`` the same way the UI's per-session
            config does — i.e. when supplied, it *replaces* the
            inherited list rather than extending it.  ``None`` means
            "inherit from session-config / global config".  An empty
            list means "no MCPs for this session" and is honoured
            verbatim (caller's intent overrides global defaults).

    Returns:
        ``(config, mcp_servers, resolution_info)`` where:

        - ``config`` is the populated :class:`ManagerConfig`.
        - ``mcp_servers`` is the dict ``{name: server_config}`` ready
          to pass to ``pool.create(..., mcp_servers=...)``.  ``None``
          means "default Claude Code tools only".
        - ``resolution_info`` carries the decisions that were made
          (working dir id/path, provider, model, MCP names, chrome
          flag, ssh_host) plus a ``persist_provider`` field — set when
          the provider was sniffed from a legacy JSONL and should be
          written back to the per-session config so future resumes are
          deterministic.  Callers handle persistence themselves (only
          meaningful when ``resume_sdk_id`` is set).
    """
    # Import lazily to avoid a circular dependency: api.routes.config
    # imports from utils.paths and pydantic; we don't want this module
    # pulling fastapi in at every call site.
    from api.routes.config import _find_active_entry, _load_config as _load_assistant_config
    from api.routes.chat import _resolve_session_provider
    from api.routes.session_config import load_session_config

    config = ManagerConfig.load()
    assistant_cfg = _load_assistant_config()
    session_cfg = load_session_config(resume_sdk_id) if resume_sdk_id else {}

    # --- Working directory + SSH ----------------------------------------
    active_entry = _resolve_working_directory(session_cfg, assistant_cfg)
    if active_entry:
        config = replace(
            config,
            project_dir=active_entry["path"],
            ssh_host=active_entry.get("ssh_host") or None,
            ssh_user=active_entry.get("ssh_user") or None,
            ssh_key=active_entry.get("ssh_key") or None,
            ssh_claude_config_dir=active_entry.get("claude_config_dir") or None,
        )
    else:
        config = replace(
            config,
            project_dir=assistant_cfg.get("working_directory", config.project_dir),
        )

    # --- Provider + harness model ---------------------------------------
    resolved_provider, resolved_model, persist_provider = _resolve_session_provider(
        resume_sdk_id=resume_sdk_id,
        session_cfg=session_cfg,
        assistant_cfg=assistant_cfg,
    )
    if resolved_provider:
        config = replace(config, provider=resolved_provider)
    if resolved_model is not None:
        config = replace(config, model=resolved_model)

    # --- MCP servers ----------------------------------------------------
    mcp_servers = _resolve_mcp_servers(
        override=mcp_override,
        session_cfg=session_cfg,
        assistant_cfg=assistant_cfg,
    )

    # --- Chrome extension flag ------------------------------------------
    chrome = session_cfg.get("chrome_extension")
    if chrome is None:
        chrome = assistant_cfg.get("chrome_extension", False)
    if chrome:
        config = replace(config, extra_args={"chrome": None})

    resolution_info: dict[str, Any] = {
        "working_directory": active_entry["id"] if active_entry else config.project_dir,
        "project_dir": config.project_dir,
        "ssh_host": config.ssh_host,
        "provider": config.provider,
        "model": config.model,
        "chrome_extension": bool(chrome),
        "mcp_servers": sorted(mcp_servers.keys()) if mcp_servers else [],
        "persist_provider": persist_provider,
    }
    return config, mcp_servers, resolution_info


def _resolve_working_directory(
    session_cfg: dict[str, Any],
    assistant_cfg: dict[str, Any],
) -> dict[str, Any] | None:
    """Pick the active working-directory entry, preferring per-session."""
    from api.routes.config import _find_active_entry

    session_wd_id = session_cfg.get("working_directory")
    if session_wd_id:
        history = assistant_cfg.get("working_directory_history", [])
        match = next((e for e in history if e["id"] == session_wd_id), None)
        if match:
            return match
        # Stale per-session entry id — fall through to global active so
        # the session still starts somewhere reasonable.
        logger.warning(
            "Session-config working_directory id %r not in global history; "
            "using global active entry instead",
            session_wd_id,
        )
    return _find_active_entry(assistant_cfg)


def _resolve_mcp_servers(
    *,
    override: list[str] | None,
    session_cfg: dict[str, Any],
    assistant_cfg: dict[str, Any],
) -> dict[str, dict] | None:
    """Compute the ``{name: config}`` MCP map for the session.

    Precedence (highest first):
      1. Caller's ``override`` — when not None, used as the authoritative
         list (matching how the UI's per-session config replaces, not
         extends, the global list).  Empty list = no MCPs.
      2. Session-config ``enabled_mcps`` (per-session override stored
         on disk).
      3. Global ``enabled_mcps`` from ``assistant_config.json``.

    Names that don't resolve to a real MCP are silently dropped (with a
    warning) by :func:`utils.mcp_config.get_mcp_configs` — the
    orchestrator's ``open_agent_session`` tool validates explicitly
    *before* calling this so the model gets a clear error.  The UI's
    enabled_mcps field is curated through the Config page so it's already
    a valid subset; dropping unknowns there is defensive only.
    """
    from utils.mcp_config import get_mcp_configs

    if override is not None:
        names = override
    else:
        raw = session_cfg.get("enabled_mcps")
        if raw is None:
            raw = assistant_cfg.get("enabled_mcps", [])
        names = raw or []

    if not names:
        return None

    resolved = get_mcp_configs(names)
    return resolved or None
