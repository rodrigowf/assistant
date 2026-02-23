"""REST session endpoints — list, get, delete, preview."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_pool, get_store
from api.models import (
    ContentBlockResponse,
    MessagePreviewResponse,
    PoolSessionResponse,
    SessionDetailResponse,
    SessionInfoResponse,
)
from api.pool import SessionPool
from manager.store import SessionStore

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _convert_blocks(blocks) -> list[ContentBlockResponse]:
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
def list_sessions(
    store: SessionStore = Depends(get_store),
    pool: SessionPool = Depends(get_pool),
):
    # Build a reverse map: sdk_session_id → local_id for all live pool sessions.
    # This lets the frontend find the correct tab for a session that the orchestrator
    # opened (where the tab is keyed by local_id, not sdk_session_id).
    sdk_to_local: dict[str, str] = {}
    for s in pool.list_sessions():
        sdk_id = s.get("sdk_session_id")
        local_id = s.get("session_id")  # pool keys sessions by local_id
        if sdk_id and local_id:
            sdk_to_local[sdk_id] = local_id

    return [
        SessionInfoResponse(
            session_id=s.session_id,
            started_at=s.started_at.isoformat(),
            last_activity=s.last_activity.isoformat(),
            title=s.title,
            message_count=s.message_count,
            is_orchestrator=s.is_orchestrator,
            local_id=sdk_to_local.get(s.session_id),
        )
        for s in store.list_sessions()
    ]


@router.get("/pool/live", response_model=list[PoolSessionResponse])
def list_pool_sessions(
    pool: SessionPool = Depends(get_pool),
    store: SessionStore = Depends(get_store),
):
    """List sessions currently live in the backend pool.

    Used by the frontend on startup to re-attach to sessions that are still
    running after a browser close/refresh.
    """
    result: list[PoolSessionResponse] = []

    # Orchestrator session (at most one)
    if pool.has_orchestrator():
        oid = pool.orchestrator_id
        session = pool.get_orchestrator()
        # The JSONL is keyed by jsonl_id (== resume_id when resuming, else local_id)
        jsonl_id = getattr(session, "jsonl_id", oid) if session else oid
        info = store.get_session_info(jsonl_id) if jsonl_id else None
        result.append(PoolSessionResponse(
            local_id=oid,
            sdk_session_id=jsonl_id,
            status="idle",
            cost=0.0,
            turns=0,
            title=info.title if info else "Orchestrator",
            is_orchestrator=True,
        ))

    # Regular agent sessions
    for s in pool.list_sessions():
        local_id = s["session_id"]
        sdk_id = s.get("sdk_session_id")
        title = None
        if sdk_id:
            info = store.get_session_info(sdk_id)
            if info:
                title = info.title
        result.append(PoolSessionResponse(
            local_id=local_id,
            sdk_session_id=sdk_id,
            status=s["status"],
            cost=s["cost"],
            turns=s["turns"],
            title=title,
            is_orchestrator=False,
        ))

    return result


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


@router.patch("/{session_id}/rename", status_code=204)
def rename_session(session_id: str, body: dict, store: SessionStore = Depends(get_store)):
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(400, detail="title is required")
    if not store.rename_session(session_id, title):
        raise HTTPException(404, detail=f"Session {session_id!r} not found")


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str, store: SessionStore = Depends(get_store)):
    if not store.delete_session(session_id):
        raise HTTPException(404, detail=f"Session {session_id!r} not found")


@router.post("/{local_id}/close", status_code=204)
async def close_pool_session(
    local_id: str,
    pool: SessionPool = Depends(get_pool),
    store: SessionStore = Depends(get_store),
):
    """Close an active session in the pool.

    If the session was genuinely new (not resumed from history) and was
    never used (zero turns), its JSONL file is deleted to prevent orphaned
    files from accumulating on disk. Resumed sessions are never deleted
    here — they have existing history that must be preserved.
    """
    sm = pool.get(local_id)
    sdk_id = sm.sdk_session_id if sm else None
    is_new_unused = (
        sm is not None
        and sm.turns == 0
        and not getattr(sm, "is_resumed", False)
    )

    if pool.has(local_id):
        await pool.close(local_id)

    # Clean up JSONL only for genuinely new sessions that were never used
    if is_new_unused and sdk_id:
        store.delete_session(sdk_id)
