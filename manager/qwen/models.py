"""Discover the model catalog the Qwen Code CLI advertises.

Qwen Code stores its model catalog in ``~/.qwen/settings.json`` under the
``modelProviders`` key.  That file is the same one the CLI itself reads, so
whatever models a user has wired up there (Qwen, DeepSeek, GLM, a local
Ollama endpoint, …) are exactly the IDs ``qwen --model <id>`` will accept.

We expose that catalog to the frontend so the Configuration UI can render a
"Harness model" dropdown whose contents reflect the user's actual install,
instead of a hardcoded list that drifts every time Qwen ships a new release.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _settings_path() -> Path:
    """Resolve ``~/.qwen/settings.json``.

    Honors ``QWEN_HOME`` for tests (so we can point at a fixture dir without
    polluting the real config), falling back to ``$HOME/.qwen``.
    """
    qwen_home = os.environ.get("QWEN_HOME")
    if qwen_home:
        return Path(qwen_home) / "settings.json"
    return Path.home() / ".qwen" / "settings.json"


@dataclass(frozen=True)
class QwenModelInfo:
    """One row in the Qwen harness model dropdown.

    Mirrors the shape we hand the frontend: ``id`` is what we pass to
    ``qwen --model``, ``display_name`` is what we show in the UI, the rest
    are informational badges (context size, vision support, "thinking" tag).
    """
    id: str
    display_name: str
    provider: str               # the key under modelProviders (e.g. "openai", "ollama")
    base_url: str | None = None
    context_window: int | None = None
    supports_vision: bool = False
    supports_video: bool = False
    supports_thinking: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider": self.provider,
            "base_url": self.base_url,
            "context_window": self.context_window,
            "supports_vision": self.supports_vision,
            "supports_video": self.supports_video,
            "supports_thinking": self.supports_thinking,
        }


def _parse_model_entry(entry: dict, provider_key: str) -> QwenModelInfo | None:
    """Normalize one ``modelProviders[<provider>][<i>]`` entry.

    Returns ``None`` for malformed rows (missing ``id``).  Anything else
    we tolerate — Qwen's settings.json schema isn't strictly versioned, so
    we accept whatever's there and just leave the missing badges blank.
    """
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None

    display_name = entry.get("name") or model_id
    base_url = entry.get("baseUrl")

    gen = entry.get("generationConfig") or {}
    context_window = gen.get("contextWindowSize")
    if not isinstance(context_window, int):
        context_window = None

    modalities = gen.get("modalities") or {}
    supports_vision = bool(modalities.get("image"))
    supports_video = bool(modalities.get("video"))

    extra = gen.get("extra_body") or {}
    supports_thinking = bool(extra.get("enable_thinking"))

    return QwenModelInfo(
        id=model_id,
        display_name=display_name,
        provider=provider_key,
        base_url=base_url if isinstance(base_url, str) else None,
        context_window=context_window,
        supports_vision=supports_vision,
        supports_video=supports_video,
        supports_thinking=supports_thinking,
    )


def list_qwen_models() -> list[QwenModelInfo]:
    """Read ``~/.qwen/settings.json`` and return its model catalog.

    Returns an empty list (not an error) if the file is missing or
    unreadable — the caller decides what to do with "no models found"
    (e.g. show an empty dropdown with a hint to run ``qwen`` once to
    seed the settings file).  Malformed JSON is logged as a warning and
    treated as "no models".
    """
    path = _settings_path()
    if not path.is_file():
        return []
    try:
        with path.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read Qwen settings.json at %s: %s", path, e)
        return []

    providers = data.get("modelProviders") or {}
    if not isinstance(providers, dict):
        return []

    out: list[QwenModelInfo] = []
    for provider_key, entries in providers.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            info = _parse_model_entry(entry, provider_key)
            if info is not None:
                out.append(info)
    return out
