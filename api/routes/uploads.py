"""File upload endpoint — receives shared/uploaded files from peripherals.

A file POSTed here is written under ``context/uploads/`` and the response
carries both the on-disk path (for the orchestrator's filesystem tools) and a
URL (``/uploads/<name>``) that resolves on the local network. The Android app
(share sheet + in-conversation upload button) is the primary caller; the
returned metadata is then sent to the orchestrator as a text turn so it can
decide what to do with the file.

The matching ``GET /uploads/<path>`` static route lives in ``api/app.py``
(registered before the SPA catch-all, same guarded pattern as ``/projects``
and ``/memory``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile

from utils.paths import get_uploads_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# 200 MB ceiling — large enough for photos/short video shared from a phone,
# small enough that a stray upload can't fill the Jetson's SD card. Enforced
# by streaming and counting bytes (UploadFile doesn't expose a reliable size
# up front for multipart).
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# Chunk size for the streamed copy — keeps peak memory flat regardless of file
# size.
_CHUNK = 1024 * 1024

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str | None) -> str:
    """Reduce an arbitrary client filename to a safe basename.

    Strips any directory components (``../`` traversal, absolute paths) and
    replaces anything outside ``[A-Za-z0-9._-]`` with ``_``. Falls back to a
    generic name when the client supplied nothing usable.
    """
    if not name:
        return "upload"
    # Basename only — drop any path the client may have smuggled in.
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._-")
    # Collapse a fully-stripped name (e.g. the client sent only "/") to a default.
    return base or "upload"


@router.post("")
async def upload_file(file: UploadFile = File(...)) -> dict:
    """Persist an uploaded file under ``context/uploads/`` and return its link.

    Response shape::

        {
          "filename": "photo.jpg",          # sanitized basename actually written
          "path": "/abs/.../context/uploads/1721-...-photo.jpg",
          "url": "/uploads/1721-...-photo.jpg",
          "size": 12345,
          "content_type": "image/jpeg"
        }

    The stored name is prefixed with a UTC timestamp so repeated uploads of the
    same client filename never clobber each other.
    """
    uploads_dir = get_uploads_dir()
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_filename(file.filename)
    # Timestamp prefix guarantees uniqueness without a DB / collision check.
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    stored_name = f"{ts}-{safe_name}"
    dest = uploads_dir / stored_name

    size = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB "
                            "upload limit."
                        ),
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("Upload write failed")
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {e}") from e
    finally:
        await file.close()

    logger.info(
        "upload stored name=%s size=%d content_type=%s",
        stored_name, size, file.content_type,
    )

    return {
        "filename": safe_name,
        "path": str(dest),
        "url": f"/uploads/{stored_name}",
        "size": size,
        "content_type": file.content_type or "application/octet-stream",
    }
