"""Tools for inspecting and updating the global assistant configuration.

The orchestrator spawns agent sessions through
:func:`api.session_factory.build_session_config`, which honours whatever
is currently saved in ``assistant_config.json`` — the same file the
Config page edits.  When the model needs a session that runs against a
different working directory, switches harness, toggles chrome, or
enables a different MCP set, the right move is to update the file
*first* (so the next ``open_agent_session`` picks it up) rather than
trying to pass overrides per-call.

These tools wrap the existing FastAPI handlers in
:mod:`api.routes.config` so validation (allowed values, SDK
availability checks, working-directory entry resolution) stays in one
place.  The model never edits raw JSON — every change goes through the
same Pydantic guardrails the HTTP UI uses.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)


@registry.register(
    name="get_assistant_config",
    description=(
        "Read the current global assistant configuration that shapes coding-agent "
        "sessions: active working directory (local or SSH target), the full "
        "working_directory_history, harness provider + per-provider model, "
        "enabled MCP servers, and chrome_extension flag. Use this BEFORE "
        "calling open_agent_session when you need to verify (or change via "
        "update_assistant_config) the settings the next session will inherit. "
        "Note: voice/orchestrator settings (default_voice_*, voice_recording_enabled, "
        "default_model, summarizer_model) may also appear in the response but are "
        "NOT editable from this tool — they belong to your own runtime."
    ),
    input_schema={
        "type": "object",
        "properties": {},
    },
)
async def get_assistant_config(context: dict[str, Any]) -> str:
    from api.routes.config import _load_config

    return json.dumps(_load_config())


@registry.register(
    name="update_assistant_config",
    description=(
        "Update one or more CODING-AGENT fields of the global assistant "
        "configuration (assistant_config.json) — the same path the Config "
        "page in the UI uses for the coding-agent section. Scope is "
        "deliberately limited: this tool only edits settings the next "
        "open_agent_session will inherit (working directory, MCPs, "
        "chrome flag, harness provider + model). Voice and orchestrator-runtime "
        "settings are intentionally NOT exposed here — changing them mid-call "
        "would disrupt your own session. "
        "All changes are validated server-side (working directory ids must exist "
        "in working_directory_history, harness provider must be registered, etc.) "
        "and take effect immediately for every subsequent open_agent_session call. "
        "Pass only the fields you want to change; omitted fields keep their "
        "current values. Returns the full updated config. "
        "Typical flow when spawning a session with non-default settings: "
        "1) call get_assistant_config to inspect, 2) call update_assistant_config "
        "with the deltas, 3) call open_agent_session — the new session inherits "
        "the new settings without per-call overrides."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "working_directory": {
                "type": "string",
                "description": (
                    "Entry id (NOT raw path) from working_directory_history to set "
                    "as active. Use get_assistant_config to list valid ids."
                ),
            },
            "enabled_mcps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Names of MCP servers to enable for new sessions by default. "
                    "Empty list = no MCPs enabled. The names must exist in the "
                    "system prompt's 'Available MCPs' section."
                ),
            },
            "chrome_extension": {
                "type": "boolean",
                "description": (
                    "When true, new sessions launch the bundled CLI with the "
                    "--chrome flag so the Chrome extension can attach."
                ),
            },
            "provider": {
                "type": "string",
                "description": (
                    "Session-harness id ('claude', 'qwen', ...) for new sessions. "
                    "Must be a registered harness."
                ),
            },
            "harness_model": {
                "type": "object",
                "description": (
                    "Per-provider harness model override, e.g. "
                    "{'claude': 'claude-sonnet-4-5-20250929'}. An empty string "
                    "means 'use CLI default for that provider'."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
    },
)
async def update_assistant_config(
    context: dict[str, Any],
    working_directory: str | None = None,
    enabled_mcps: list[str] | None = None,
    chrome_extension: bool | None = None,
    provider: str | None = None,
    harness_model: dict[str, str] | None = None,
) -> str:
    # Build a kwargs dict of only the fields the caller actually supplied
    # (i.e. everything that isn't None).  The signature must declare each
    # field explicitly because the registry executor filters call-site
    # kwargs by the handler's named parameters — ``**fields`` would
    # collapse to a single VAR_KEYWORD slot and lose them all.
    #
    # Coding-agent scope only: voice + orchestrator-runtime fields
    # (default_voice_*, voice_recording_enabled, default_model,
    # summarizer_model) are intentionally not in this signature so the
    # orchestrator can't disrupt its own session mid-call.
    fields = {
        k: v for k, v in {
            "working_directory": working_directory,
            "enabled_mcps": enabled_mcps,
            "chrome_extension": chrome_extension,
            "provider": provider,
            "harness_model": harness_model,
        }.items() if v is not None
    }
    if not fields:
        return json.dumps({
            "error": "No fields supplied. Pass at least one field to update.",
        })

    # Re-use the route handler so validation + cascade rules (e.g. snapping
    # voice model defaults when the provider changes) match the UI byte
    # for byte.  Pydantic raises ValidationError on unknown fields/types,
    # which we surface as a structured error rather than letting it bubble.
    from fastapi import HTTPException
    from api.routes.config import ConfigUpdate, update_config

    try:
        body = ConfigUpdate(**fields)
    except Exception as e:
        return json.dumps({"error": f"Invalid field(s): {e}"})

    try:
        updated = await update_config(body)
    except HTTPException as e:
        return json.dumps({"error": e.detail})
    except Exception as e:
        logger.exception("update_assistant_config failed")
        return json.dumps({"error": str(e)})

    return json.dumps(updated)
