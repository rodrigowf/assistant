"""run_script tool — run a curated, allowlisted script locally via run.sh.

The allowlist lives in `context/memory/ORCHESTRATOR_SCRIPTS.md` (next to the
orchestrator memory file). It is plain markdown that the orchestrator curates
itself with read_file/write_file, and that Claude sessions update when they
create new reusable scripts. Each allowed script is declared by a `path:` line
inside a fenced block; this module parses those lines to build the allowlist.

Scripts run through `context/scripts/run.sh` (the project venv) with no
isolation. This tool is for SINGLE self-contained script calls only — anything
that is an actual task, is open-ended, or needs more than one script call
chained together must be delegated to a Claude session instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any

from orchestrator.tools import registry

logger = logging.getLogger(__name__)

# Paths.
_PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
_RUN_SH = _PROJECT_DIR / "context" / "scripts" / "run.sh"
_ALLOWLIST_FILE = _PROJECT_DIR / "context" / "memory" / "ORCHESTRATOR_SCRIPTS.md"

# Max seconds a script may run before it is killed.
_TIMEOUT_SECONDS = 300
# Cap captured output so a chatty script can't blow up the prompt.
_MAX_OUTPUT_CHARS = 20_000

# Matches a `path: <script path>` line inside the markdown allowlist. Leading
# whitespace and surrounding backticks/quotes are tolerated.
_PATH_LINE_RE = re.compile(r"^\s*path:\s*[`'\"]?([^`'\"\s]+)[`'\"]?\s*$", re.MULTILINE)


def _load_allowed_paths() -> set[str]:
    """Parse the allowlist markdown and return the set of allowed script paths.

    Paths are normalised to be relative to the project root (a leading
    `./` or absolute project-root prefix is stripped) so they compare cleanly
    against what the caller passes.
    """
    if not _ALLOWLIST_FILE.is_file():
        return set()
    try:
        text = _ALLOWLIST_FILE.read_text(encoding="utf-8")
    except Exception:
        logger.exception("Failed to read script allowlist %s", _ALLOWLIST_FILE)
        return set()
    return {_normalize(m) for m in _PATH_LINE_RE.findall(text)}


def _normalize(path: str) -> str:
    """Normalise a script path to a project-root-relative string."""
    p = path.strip()
    try:
        resolved = (_PROJECT_DIR / Path(p).expanduser()).resolve()
        return str(resolved.relative_to(_PROJECT_DIR))
    except (ValueError, OSError):
        # Absolute path outside the project, or unresolvable — return as-is so
        # it simply won't match any allowlist entry.
        return p


def _clip(text: str) -> str:
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    return text[:_MAX_OUTPUT_CHARS] + f"\n... [truncated at {_MAX_OUTPUT_CHARS} chars]"


@registry.register(
    name="run_script",
    description=(
        "Run ONE allowlisted script locally and capture its output. Only scripts "
        "listed in context/memory/ORCHESTRATOR_SCRIPTS.md (injected into your "
        "prompt) can be run — pass `script` exactly as its `path:` line reads. "
        "This is for a single self-contained script call (e.g. toggle a lamp, "
        "generate one image). If the request is an actual task, is open-ended, "
        "needs judgment, or needs more than one script call chained together, do "
        "NOT use this — delegate to a Claude session via open_agent_session + "
        "send_to_agent_session. Returns exit_code, stdout, and stderr."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": (
                    "Path of the script to run, exactly as written in its "
                    "allowlist `path:` line (e.g. 'context/scripts/tuya_lamps.py')."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Command-line arguments passed to the script, in order. "
                    "Each element is one argument (e.g. ['off', 'all'])."
                ),
            },
        },
        "required": ["script"],
    },
)
async def run_script(
    context: dict[str, Any],
    script: str,
    args: list[str] | None = None,
) -> str:
    args = args or []

    allowed = _load_allowed_paths()
    normalized = _normalize(script)

    if normalized not in allowed:
        return json.dumps({
            "error": (
                f"Script '{script}' is not in the allowlist. Only scripts listed "
                "in ORCHESTRATOR_SCRIPTS.md can be run. Add an entry there with "
                "write_file if this script should be runnable, or delegate the "
                "task to a Claude session."
            ),
            "allowed": sorted(allowed),
        })

    target = (_PROJECT_DIR / normalized).resolve()
    if not target.is_file():
        return json.dumps({
            "error": (
                f"Script is allowlisted but the file does not exist on disk: "
                f"{normalized}. The allowlist may be stale — fix it with write_file."
            ),
        })

    # Coerce all args to strings — the model occasionally emits numbers.
    str_args = [str(a) for a in args]
    cmd = [str(_RUN_SH), str(target), *str_args]

    logger.info("run_script: %s %s", normalized, str_args)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return json.dumps({
            "error": f"Script timed out after {_TIMEOUT_SECONDS}s",
            "script": normalized,
        })
    except Exception as e:
        return json.dumps({"error": f"Failed to launch script: {e}", "script": normalized})

    return json.dumps({
        "script": normalized,
        "args": str_args,
        "exit_code": proc.returncode,
        "stdout": _clip(stdout.decode(errors="replace").strip()),
        "stderr": _clip(stderr.decode(errors="replace").strip()),
    })
