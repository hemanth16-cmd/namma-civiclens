"""Staff console API: sign-in, complaint list and detail, private evidence viewing with an audit log.

Every route except sign-in requires a staff bearer session (auth.current_staff). Evidence is
streamed only through GET /staff/complaints/{id}/evidence after the session is checked and the
view is written to evidence_access_log; there are no public, permanent or signed links.
"""

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from .auth import current_staff, normalise_username, session_ttl, sign_in
from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .models import EvidenceAccessEntry, StaffComplaint, StaffComplaintList, StaffIdentity, StaffSessionOut, StaffSignIn
from .ratelimit import client_ip, enforce
from .store import STAFF_LIST_MAX, StoreError

router = APIRouter(prefix="/staff", tags=["staff"])
CHUNK = 64 * 1024
EXTENSIONS = {"image/jpeg": "jpg", "video/webm": "webm", "video/mp4": "mp4"}


@router.post("/sessions", response_model=StaffSessionOut)
def create_session(payload: StaffSignIn, request: Request, response: Response) -> dict:
    """Sign in. Rate-limited per IP and per username before the password check."""
    enforce(request, "staff_login_per_ip", "ip")
    enforce(request, "staff_login_per_account", "account:" + normalise_username(payload.username))
    session = sign_in(request.app.state.store, payload.username, payload.password, request.app.state.staff_session_ttl)
    if session is None:
        raise ApiProblem(401, "unauthorized", "The username or password is incorrect.")
    response.headers["Cache-Control"] = "no-store"
    return session


@router.delete("/sessions/current", status_code=204)
def end_session(request: Request, staff: dict = Depends(current_staff)) -> Response:
    request.app.state.store.revoke_staff_session(staff["tokenHash"])
    return Response(status_code=204)


@router.get("/me", response_model=StaffIdentity)
def who_am_i(staff: dict = Depends(current_staff)) -> dict:
    return staff


def _summary(record: dict) -> dict:
    return {k: record[k] for k in ("id", "category", "area", "status", "verification", "created", "updated", "mediaType")} | {"hasEvidence": record["evidence"] is not None}


@router.get("/complaints", response_model=StaffComplaintList)
def list_complaints(request: Request, response: Response, staff: dict = Depends(current_staff),
                    limit: int = Query(50, ge=1, le=STAFF_LIST_MAX), offset: int = Query(0, ge=0)) -> dict:
    records, total = request.app.state.store.list_complaints(limit, offset)
    response.headers["Cache-Control"] = "no-store"
    return {"items": [_summary(r) for r in records], "total": total, "limit": limit, "offset": offset}


def _record_or_404(request: Request, public_id: str) -> dict:
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    record = request.app.state.store.get_staff_complaint(public_id)
    if record is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    return record


@router.get("/complaints/{public_id}", response_model=StaffComplaint)
def get_complaint(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return _record_or_404(request, public_id)


@router.get("/complaints/{public_id}/evidence")
def view_evidence(public_id: str, request: Request, staff: dict = Depends(current_staff)) -> StreamingResponse:
    """Stream the original evidence to signed-in staff. The view is logged before any byte is sent;
    if logging fails, nothing is sent."""
    store = request.app.state.store
    evidence = _record_or_404(request, public_id)["evidence"]
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", "No evidence has been uploaded for this complaint.")
    # Read from the backend that stored this file (local evidence stays readable after switching to Supabase Storage).
    storage = request.app.state.evidence_storages.get(evidence["storageBackend"])
    if storage is None:
        raise StoreError(f"Evidence is in the {evidence['storageBackend']!r} backend, which is not configured")
    handle = storage.open_read(evidence["storageKey"])  # StoreError → 503
    try:
        store.log_evidence_access(evidence["pk"], staff["pk"], client_ip(request, request.app.state.rate_limit_settings.trusted_proxies))
    except BaseException:
        handle.close()
        raise

    def chunks():
        try:
            while data := handle.read(CHUNK):
                yield data
        finally:
            handle.close()

    filename = f"{public_id}-evidence.{EXTENSIONS.get(evidence['contentType'], 'bin')}"
    return StreamingResponse(chunks(), media_type=evidence["contentType"], headers={
        "Content-Length": str(evidence["byteSize"]),
        "Content-Disposition": f'inline; filename="{filename}"',
        "Cache-Control": "no-store, private",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.get("/complaints/{public_id}/evidence/access", response_model=list[EvidenceAccessEntry])
def evidence_access_log(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    evidence = _record_or_404(request, public_id)["evidence"]
    response.headers["Cache-Control"] = "no-store"
    if evidence is None:
        return []
    return [{"username": r["username"], "displayName": r["display_name"], "action": r["action"], "accessedAt": r["accessed_at"]}
            for r in request.app.state.store.list_evidence_access(evidence["pk"])]
