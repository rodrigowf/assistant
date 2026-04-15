"""Skills discovery endpoint — lists available skills from the skills directory."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from utils.paths import PROJECT_ROOT, parse_md_frontmatter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/skills", tags=["skills"])


def _get_skills_dir() -> Path:
    """Return the skills directory (context/skills symlinks)."""
    ctx_skills = PROJECT_ROOT / "context" / "skills"
    if ctx_skills.is_dir():
        return ctx_skills
    return PROJECT_ROOT / "default-skills"


def _load_skill_info(skill_dir: Path) -> dict[str, Any] | None:
    """Parse SKILL.md frontmatter to get name and description."""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return None
    try:
        content = skill_md.read_text()
    except IOError:
        return None
    name, description = parse_md_frontmatter(content, skill_dir.name)
    return {"name": name, "description": description, "dir": skill_dir.name}


@router.get("")
async def list_skills() -> dict[str, Any]:
    """List all available skills."""
    skills_dir = _get_skills_dir()
    skills: list[dict[str, Any]] = []

    if not skills_dir.is_dir():
        return {"skills": skills}

    for entry in sorted(skills_dir.iterdir()):
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
