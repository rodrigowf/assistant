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


class AuthStatusResponse(BaseModel):
    authenticated: bool


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None
