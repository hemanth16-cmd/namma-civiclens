"""Staff console API: sign-in, complaint list and detail, private evidence viewing with an audit log.

Every route except sign-in requires a staff bearer session (auth.current_staff). Evidence is
streamed only through GET /staff/complaints/{id}/evidence after the session is checked and the
view is written to evidence_access_log; there are no public, permanent or signed links.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from .auth import current_staff, normalise_username, session_ttl, sign_in
from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .models import EvidenceAccessEntry, StaffCase, StaffComplaintList, StaffIdentity, StaffSessionOut, StaffSignIn
from .evidence_access import open_and_stream
from .ratelimit import client_ip, enforce
from .store import STAFF_LIST_MAX

router = APIRouter(prefix="/staff", tags=["staff"])
logger = logging.getLogger("civiclens.staff")
# 404 evidence_unavailable messages: the record exists but this server cannot serve its file.
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


@router.get("/complaints", response_model=StaffComplaintList)
def list_complaints(request: Request, response: Response, staff: dict = Depends(current_staff),
                    limit: int = Query(50, ge=1, le=STAFF_LIST_MAX), offset: int = Query(0, ge=0)) -> dict:
    records, total = request.app.state.store.list_complaints(limit, offset, staff_pk=staff["pk"])
    response.headers["Cache-Control"] = "no-store"
    return {"items": records, "total": total, "limit": limit, "offset": offset}


def _record_or_404(request: Request, public_id: str, staff: dict) -> dict:
    """The staff record, or 404. Officials outside the complaint's departments get 403 (lifecycle.in_scope)."""
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    store = request.app.state.store
    record = store.get_staff_complaint(public_id)
    if record is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    store.require_case_scope(public_id, staff["pk"])
    return record


@router.get("/complaints/{public_id}", response_model=StaffCase)
def get_complaint(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    """The staff view plus management data (assignment, work orders, internal notes, allowed actions)."""
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    record = request.app.state.store.case_detail(public_id, staff["pk"])
    if record is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    response.headers["Cache-Control"] = "no-store"
    return record


@router.get("/complaints/{public_id}/evidence")
def view_evidence(public_id: str, request: Request, background: BackgroundTasks, staff: dict = Depends(current_staff)) -> StreamingResponse:
    """Stream the original evidence to signed-in staff. The view is logged before any byte is sent;
    if logging fails, nothing is sent."""
    evidence = _record_or_404(request, public_id, staff)["evidence"]
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", "No evidence has been uploaded for this complaint.")
    streamed = stream_evidence(request, evidence, staff, f"{public_id}-evidence", background)
    background.add_task(request.app.state.notifier.evidence_viewed, public_id)  # only once the view is logged; followers are told once
    return streamed


def stream_evidence(request: Request, evidence: dict, staff: dict, basename: str, background: BackgroundTasks | None = None) -> StreamingResponse:
    """Stream one evidence file to signed-in staff through the shared retrieval service. The view is
    written to evidence_access_log before any byte is sent; if logging fails, nothing is sent.
    `evidence` needs pk, storageBackend, storageKey, contentType, byteSize."""
    def log_view() -> None:
        request.app.state.store.log_evidence_access(evidence["pk"], staff["pk"], client_ip(request, request.app.state.rate_limit_settings.trusted_proxies))
    return open_and_stream(request, evidence, basename, log_view, background)


@router.get("/complaints/{public_id}/evidence/access", response_model=list[EvidenceAccessEntry])
def evidence_access_log(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    evidence = _record_or_404(request, public_id, staff)["evidence"]
    response.headers["Cache-Control"] = "no-store"
    if evidence is None:
        return []
    return [{"username": r["username"], "displayName": r["display_name"], "action": r["action"], "accessedAt": r["accessed_at"]}
            for r in request.app.state.store.list_evidence_access(evidence["pk"])]
