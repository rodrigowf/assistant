"""Pydantic request/response models for the API."""

from __future__ import annotations

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

class SessionInfoResponse(BaseModel):
    session_id: str
    started_at: str
    last_activity: str
    title: str
    message_count: int
    is_orchestrator: bool = False


class ContentBlockResponse(BaseModel):
    type: str  # "text" | "tool_use" | "tool_result"
    text: str | None = None
    tool_use_id: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    output: str | None = None
    is_error: bool = False


class MessagePreviewResponse(BaseModel):
    role: str
    text: str
    blocks: list[ContentBlockResponse] = []
    timestamp: str | None = None


class SessionDetailResponse(SessionInfoResponse):
    messages: list[MessagePreviewResponse] = []


class PoolSessionResponse(BaseModel):
    """A session that is currently live in the pool (not just in JSONL history)."""
    local_id: str
    sdk_session_id: str | None = None
    status: str
    cost: float
    turns: int
    title: str | None = None
    is_orchestrator: bool = False


class AuthStatusResponse(BaseModel):
    authenticated: bool


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
