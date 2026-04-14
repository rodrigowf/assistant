"""Configuration for the orchestrator agent.

Supports runtime model switching between Anthropic and OpenAI providers.
The provider/model can be changed mid-conversation for text and turn-based
interactions. Realtime voice sessions use a fixed model (set at session start).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from utils.paths import get_memory_dir


# ---------------------------------------------------------------------------
# Provider and Model Definitions
# ---------------------------------------------------------------------------

class Provider(str, Enum):
    """Supported model providers."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """Information about an available model."""

    provider: Provider
    model_id: str
    display_name: str
    supports_audio: bool = False
    supports_vision: bool = False
    supports_tools: bool = True
    max_tokens: int = 8192

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "provider": self.provider.value,
            "model_id": self.model_id,
            "display_name": self.display_name,
            "supports_audio": self.supports_audio,
            "supports_vision": self.supports_vision,
            "supports_tools": self.supports_tools,
            "max_tokens": self.max_tokens,
        }


# Available models registry
AVAILABLE_MODELS: dict[str, ModelInfo] = {
    # Anthropic models
    "claude-sonnet-4-5-20250929": ModelInfo(
        provider=Provider.ANTHROPIC,
        model_id="claude-sonnet-4-5-20250929",
        display_name="Claude Sonnet 4.5",
        supports_vision=True,
        max_tokens=8192,
    ),
    "claude-opus-4-20250514": ModelInfo(
        provider=Provider.ANTHROPIC,
        model_id="claude-opus-4-20250514",
        display_name="Claude Opus 4",
        supports_vision=True,
        max_tokens=8192,
    ),
    "claude-haiku-3-5-20241022": ModelInfo(
        provider=Provider.ANTHROPIC,
        model_id="claude-haiku-3-5-20241022",
        display_name="Claude Haiku 3.5",
        supports_vision=True,
        max_tokens=4096,
    ),
    # OpenAI models
    # Note: gpt-4o does NOT support audio input - use gpt-4o-audio-preview for audio
    "gpt-4o": ModelInfo(
        provider=Provider.OPENAI,
        model_id="gpt-4o",
        display_name="GPT-4o",
        supports_audio=False,  # Use gpt-4o-audio-preview for audio
        supports_vision=True,
        max_tokens=16384,
    ),
    "gpt-4o-audio-preview": ModelInfo(
        provider=Provider.OPENAI,
        model_id="gpt-4o-audio-preview",
        display_name="GPT-4o Audio",
        supports_audio=True,
        supports_vision=True,
        max_tokens=16384,
    ),
    "gpt-4o-mini": ModelInfo(
        provider=Provider.OPENAI,
        model_id="gpt-4o-mini",
        display_name="GPT-4o Mini",
        supports_audio=False,  # Use gpt-4o-mini-audio-preview for audio
        supports_vision=True,
        max_tokens=16384,
    ),
    "gpt-4o-mini-audio-preview": ModelInfo(
        provider=Provider.OPENAI,
        model_id="gpt-4o-mini-audio-preview",
        display_name="GPT-4o Mini Audio",
        supports_audio=True,
        supports_vision=True,
        max_tokens=16384,
    ),
    "gpt-4-turbo": ModelInfo(
        provider=Provider.OPENAI,
        model_id="gpt-4-turbo",
        display_name="GPT-4 Turbo",
        supports_vision=True,
        max_tokens=4096,
    ),
}

# Default model
DEFAULT_MODEL_ID = "claude-sonnet-4-5-20250929"


def get_available_models() -> list[ModelInfo]:
    """Get list of all available models."""
    return list(AVAILABLE_MODELS.values())


def get_model_info(model_id: str) -> ModelInfo | None:
    """Get info for a specific model."""
    return AVAILABLE_MODELS.get(model_id)


def get_models_by_provider(provider: Provider) -> list[ModelInfo]:
    """Get all models for a specific provider."""
    return [m for m in AVAILABLE_MODELS.values() if m.provider == provider]


def get_audio_capable_models() -> list[ModelInfo]:
    """Get models that support audio input."""
    return [m for m in AVAILABLE_MODELS.values() if m.supports_audio]


# ---------------------------------------------------------------------------
# Orchestrator Configuration
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class OrchestratorConfig:
    """Configuration for an orchestrator agent session.

    Supports runtime model switching via set_model(). The provider is
    automatically determined from the model.

    Attributes:
        model: Current model ID (e.g., "claude-sonnet-4-5-20250929", "gpt-4o")
        max_tokens: Maximum tokens for model response
        project_dir: Base project directory
        memory_path: Path to orchestrator memory file
    """

    model: str = DEFAULT_MODEL_ID
    max_tokens: int = 8192
    project_dir: str = ""
    memory_path: str = ""

    # Runtime state (not persisted)
    _model_info: ModelInfo | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize model info from model ID."""
        self._model_info = AVAILABLE_MODELS.get(self.model)
        if self._model_info is None:
            # Default to Anthropic for unknown models
            self._model_info = ModelInfo(
                provider=Provider.ANTHROPIC,
                model_id=self.model,
                display_name=self.model,
            )

    @property
    def provider(self) -> Provider:
        """Get the provider for the current model."""
        return self._model_info.provider if self._model_info else Provider.ANTHROPIC

    @property
    def model_info(self) -> ModelInfo | None:
        """Get full model info for current model."""
        return self._model_info

    @property
    def supports_audio(self) -> bool:
        """Whether current model supports audio input."""
        return self._model_info.supports_audio if self._model_info else False

    def set_model(self, model_id: str) -> bool:
        """Change the current model.

        Args:
            model_id: The model identifier to switch to

        Returns:
            True if model was found and set, False if unknown model
        """
        info = AVAILABLE_MODELS.get(model_id)
        if info is None:
            return False

        self.model = model_id
        self._model_info = info
        self.max_tokens = min(self.max_tokens, info.max_tokens)
        return True

    def to_dict(self) -> dict[str, Any]:
        """Convert config to JSON-serializable dict."""
        return {
            "model": self.model,
            "provider": self.provider.value,
            "max_tokens": self.max_tokens,
            "supports_audio": self.supports_audio,
            "model_info": self._model_info.to_dict() if self._model_info else None,
        }

    @classmethod
    def load(cls) -> OrchestratorConfig:
        """Load config from environment variables and defaults."""
        project_dir = os.environ.get(
            "ORCHESTRATOR_PROJECT_DIR",
            str(Path(__file__).resolve().parent.parent),
        )

        # Use context/memory/ directly for the orchestrator memory file
        memory_path = str(get_memory_dir() / "ORCHESTRATOR_MEMORY.md")

        model = os.environ.get("ORCHESTRATOR_MODEL", DEFAULT_MODEL_ID)
        max_tokens = int(os.environ.get("ORCHESTRATOR_MAX_TOKENS", "8192"))

        return cls(
            model=model,
            max_tokens=max_tokens,
            project_dir=project_dir,
            memory_path=memory_path,
        )
