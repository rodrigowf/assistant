"""REST session endpoints — list, get, delete, preview."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_store
from api.models import (
    ContentBlockResponse,
    MessagePreviewResponse,
    SessionDetailResponse,
    SessionInfoResponse,
)
from manager.store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _convert_blocks(blocks) -> list[ContentBlockResponse]:
    """Convert ContentBlock list to ContentBlockResponse list."""
    return [
        ContentBlockResponse(
            type=b.type,
            text=b.text,
            tool_use_id=b.tool_use_id,
            tool_name=b.tool_name,
            tool_input=b.tool_input,
            output=b.output,
            is_error=b.is_error,
        )
        for b in blocks
    ]


@router.get("", response_model=list[SessionInfoResponse])
def list_sessions(store: SessionStore = Depends(get_store)):
    return [
        SessionInfoResponse(
            session_id=s.session_id,
            started_at=s.started_at.isoformat(),
            last_activity=s.last_activity.isoformat(),
            title=s.title,
            message_count=s.message_count,
            is_orchestrator=s.is_orchestrator,
        )
        for s in store.list_sessions()
    ]


@router.get("/{session_id}", response_model=SessionDetailResponse)
def get_session(session_id: str, store: SessionStore = Depends(get_store)):
    detail = store.get_session(session_id)
    if detail is None:
        raise HTTPException(404, detail=f"Session {session_id!r} not found")
    return SessionDetailResponse(
        session_id=detail.session_id,
        started_at=detail.started_at.isoformat(),
        last_activity=detail.last_activity.isoformat(),
        title=detail.title,
        message_count=detail.message_count,
        messages=[
            MessagePreviewResponse(
                role=m.role,
                text=m.text,
                blocks=_convert_blocks(m.blocks),
                timestamp=m.timestamp.isoformat() if m.timestamp else None,
            )
            for m in detail.messages
        ],
    )


@router.get("/{session_id}/preview", response_model=list[MessagePreviewResponse])
def get_preview(
    session_id: str,
    max_messages: int = Query(5, alias="max", ge=1, le=50),
    store: SessionStore = Depends(get_store),
):
    previews = store.get_preview(session_id, max_messages=max_messages)
    if not previews and store.get_session(session_id) is None:
        raise HTTPException(404, detail=f"Session {session_id!r} not found")
    return [
        MessagePreviewResponse(
            role=m.role,
            text=m.text,
            blocks=_convert_blocks(m.blocks),
            timestamp=m.timestamp.isoformat() if m.timestamp else None,
        )
        for m in previews
    ]


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str, store: SessionStore = Depends(get_store)):
    if not store.delete_session(session_id):
        raise HTTPException(404, detail=f"Session {session_id!r} not found")
