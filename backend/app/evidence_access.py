"""Shared evidence retrieval: one implementation for every caller that may view an uploaded file.

Moved unchanged out of staff_api.py so that staff and complaint owners use the same evidence metadata,
storage-backend selection (local disk or private Supabase Storage), file opening and streaming. Only
authorisation and the access record differ, and the caller supplies both:

- staff (staff_api.py, cases_api.py): a staff session and role checks; the view goes to evidence_access_log;
- owners (owner_evidence.py): the private claim code, or a resident session that owns the complaint; the
  view goes to audit_log.

`record_access` runs after the file has been opened and before any byte is sent. If it raises, the file
is closed and nothing is sent. No storage URL or key ever leaves the server: the bytes are streamed.
"""

import logging
from collections.abc import Callable

from fastapi import BackgroundTasks, Request
from fastapi.responses import StreamingResponse

from .errors import ApiProblem
from .store import StoreError

logger = logging.getLogger("civiclens.evidence")
CHUNK = 64 * 1024
EXTENSIONS = {"image/jpeg": "jpg", "video/webm": "webm", "video/mp4": "mp4"}

UNAVAILABLE = {
    "supabase": "This evidence is stored in Supabase Storage, which this server is not connected to, so it cannot be shown here.",
    "local": "This evidence is stored on a server's local disk that this server cannot reach, so it cannot be shown here.",
    "other": "This evidence is stored in a location this server is not connected to, so it cannot be shown here.",
}
MISSING_FILE = ("The evidence file for this complaint is not in this server's storage. It may have been uploaded while the "
                "API was running elsewhere, for example on another machine's local disk. Nothing was opened or logged.")


def _definitely_missing(storage, key: str) -> bool:
    """True only when the storage confirms the object is absent; False when it cannot tell."""
    try:
        return not storage.exists(key)
    except StoreError:
        return False


def open_and_stream(request: Request, evidence: dict, basename: str, record_access: Callable[[], None],
                    background: BackgroundTasks | None = None) -> StreamingResponse:
    """Open one evidence file in the backend that stored it and stream it. `evidence` needs storageBackend,
    storageKey, contentType and byteSize. The caller has already authorised the request."""
    # Read from the backend that stored this file (local evidence stays readable after switching to Supabase Storage).
    storage = request.app.state.evidence_storages.get(evidence["storageBackend"])
    if storage is None:
        logger.warning("Evidence for %s is in the %r backend, which this server is not configured for", basename, evidence["storageBackend"])
        raise ApiProblem(404, "evidence_unavailable", UNAVAILABLE.get(evidence["storageBackend"], UNAVAILABLE["other"]))
    try:
        handle = storage.open_read(evidence["storageKey"])
    except StoreError:
        # A file that is definitely absent is a permanent condition, not an outage: say so instead of 503.
        if _definitely_missing(storage, evidence["storageKey"]):
            logger.warning("Evidence file for %s is missing from the %r backend", basename, evidence["storageBackend"])
            raise ApiProblem(404, "evidence_unavailable", MISSING_FILE)
        raise  # → 503 storage_unavailable (e.g. Supabase unreachable)
    try:
        record_access()
    except BaseException:
        handle.close()
        raise

    def chunks():
        try:
            while data := handle.read(CHUNK):
                yield data
        finally:
            handle.close()

    filename = f"{basename}.{EXTENSIONS.get(evidence['contentType'], 'bin')}"
    return StreamingResponse(chunks(), media_type=evidence["contentType"], background=background, headers={
        "Content-Length": str(evidence["byteSize"]),
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })
