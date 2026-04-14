"""Model providers for the orchestrator agent.

Providers implement the ModelProvider protocol and translate between
the orchestrator's event system and various model APIs.

Available providers:
- AnthropicProvider: Claude models via Anthropic API
- OpenAITextProvider: GPT-4 family with multimodal (audio) support
- OpenAIVoiceProvider: OpenAI Realtime API for WebRTC voice
"""

from orchestrator.providers.anthropic import AnthropicProvider
from orchestrator.providers.openai_text import (
    OpenAIModel,
    OpenAITextProvider,
    AudioContent,
    create_audio_message,
)
from orchestrator.providers.openai_voice import OpenAIVoiceProvider

__all__ = [
    "AnthropicProvider",
    "OpenAIModel",
    "OpenAITextProvider",
    "OpenAIVoiceProvider",
    "AudioContent",
    "create_audio_message",
]
