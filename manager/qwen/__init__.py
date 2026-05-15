"""Qwen Code harness — session manager + JSONL adapter + model catalog.

Public surface re-exported here for callers that still import from
``manager.qwen``; the canonical dispatch path is through
:mod:`manager.registry`.
"""

from .adapter import QwenAdapter
from .models import list_qwen_models
from .session import QwenAbandoned, QwenSessionManager

__all__ = [
    "QwenAdapter",
    "QwenSessionManager",
    "QwenAbandoned",
    "list_qwen_models",
]
