"""Skills discovery endpoint — lists available skills from the skills directory."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["skills"])


def _get_skills_dir() -> Path:
    """Return the skills directory (context/skills symlinks)."""
    project_root = Path(__file__).resolve().parent.parent.parent
    # First try context/skills (has symlinks to all skills)
    ctx_skills = project_root / "context" / "skills"
    if ctx_skills.is_dir():
        return ctx_skills
    # Fallback: default-skills
    return project_root / "default-skills"


def _load_skill_info(skill_dir: Path) -> dict[str, Any] | None:
    """Parse SKILL.md frontmatter to get name and description."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None

    try:
        content = skill_md.read_text()
    except IOError:
        return None

    name = skill_dir.name
    description = ""

    # Parse YAML frontmatter between --- delimiters
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter = parts[1]
            for line in frontmatter.splitlines():
                line = line.strip()
                if line.startswith("description:"):
                    description = line[len("description:"):].strip()
                    # Remove surrounding quotes if any
                    if description.startswith(("'", '"')) and description.endswith(("'", '"')):
                        description = description[1:-1]
                elif line.startswith("name:"):
                    name = line[len("name:"):].strip()

    return {"name": name, "description": description, "dir": skill_dir.name}


@router.get("")
async def list_skills() -> dict[str, Any]:
    """List all available skills."""
    skills_dir = _get_skills_dir()
    skills: list[dict[str, Any]] = []

    if not skills_dir.is_dir():
        return {"skills": skills}

    for entry in sorted(skills_dir.iterdir()):
        # Resolve symlinks to get the actual directory
        try:
            resolved = entry.resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        info = _load_skill_info(resolved)
        if info is not None:
            skills.append(info)

    return {"skills": skills}
