"""Google Gemini CLI harness — session manager + JSONL adapter.

Public surface re-exported here for callers that still import from
``manager.gemini``; the canonical dispatch path is through
:mod:`manager.registry`.
"""

from .adapter import GeminiAdapter
from .session import GeminiAbandoned, GeminiSessionManager

__all__ = [
    "GeminiAdapter",
    "GeminiSessionManager",
    "GeminiAbandoned",
]
