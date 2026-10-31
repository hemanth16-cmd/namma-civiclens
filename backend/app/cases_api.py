"""Complaint-management API for signed-in staff (Workstream 1): lifecycle transitions, assignment,
internal notes, public updates, reinspection, work orders with after-repair evidence, and the audit
trail. Every route needs a staff bearer session (auth.current_staff). The rules, including role and
department checks, live in lifecycle.py and are applied by store.py inside the transaction that
locks the complaint, so the server is always the authority; the console only shows what
`allowedActions` offers.

Mutations return the updated staff view of the complaint (StaffCase) so the console can re-render
from server data. Write methods are POST only, matching the CORS allowlist (GET, POST, DELETE).
"""

import re
import secrets

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from .auth import current_staff
from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .evidence import UploadSettings, receive_upload
from .evidence_storage import EvidenceStorage
from .models import (AssignmentIn, AuditEntry, DepartmentOut, NoteIn, PublicUpdateIn, ReinspectionIn, RepairEvidenceReceipt,
                     StaffCase, TransitionIn, WorkOrderChangeIn, WorkOrderIn, WorkOrderList)
from .quota import EvidenceQuota
from .ratelimit import client_ip, enforce
from .staff_api import stream_evidence
from .store import STAFF_LIST_MAX, ComplaintStore, utc_now

router = APIRouter(prefix="/staff", tags=["complaint management"])
WORK_ORDER_ID = re.compile(r"^wo_[A-Za-z0-9_-]{16}$")
EVIDENCE_ID = re.compile(r"^ev_[A-Za-z0-9_-]{22}$")
WORK_ORDER_NOT_FOUND = "No work order was found with that ID."


def _store(request: Request) -> ComplaintStore:
    return request.app.state.store


def _ip(request: Request) -> str:
    return client_ip(request, request.app.state.rate_limit_settings.trusted_proxies)


def _check_id(public_id: str) -> None:
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)


def _check_order_id(work_order_id: str) -> None:
    if not WORK_ORDER_ID.fullmatch(work_order_id):
        raise ApiProblem(404, "not_found", WORK_ORDER_NOT_FOUND)


def case_view(request: Request, response: Response, public_id: str, staff: dict) -> dict:
    record = _store(request).case_detail(public_id, staff["pk"])
    if record is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    response.headers["Cache-Control"] = "no-store"
    return record


@router.post("/complaints/{public_id}/transitions", response_model=StaffCase)
def transition(public_id: str, payload: TransitionIn, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).transition(public_id, staff["pk"], payload.to, payload.expectedStatus, payload.explanation, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/complaints/{public_id}/assignment", response_model=StaffCase)
def assign(public_id: str, payload: AssignmentIn, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).assign(public_id, staff["pk"], payload.department, payload.assignee, payload.reason, payload.expectedStatus, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/complaints/{public_id}/notes", status_code=201, response_model=StaffCase)
def add_note(public_id: str, payload: NoteIn, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).add_note(public_id, staff["pk"], payload.body, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/complaints/{public_id}/updates", status_code=201, response_model=StaffCase)
def add_public_update(public_id: str, payload: PublicUpdateIn, request: Request, response: Response,
                      staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).add_public_update(public_id, staff["pk"], payload.text, payload.kind, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/complaints/{public_id}/reinspections", response_model=StaffCase)
def request_reinspection(public_id: str, payload: ReinspectionIn, request: Request, response: Response,
                         staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).request_reinspection(public_id, staff["pk"], payload.note, payload.reopen, payload.expectedStatus, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/complaints/{public_id}/work-orders", status_code=201, response_model=StaffCase)
def create_work_order(public_id: str, payload: WorkOrderIn, request: Request, response: Response,
                      staff: dict = Depends(current_staff)) -> dict:
    _check_id(public_id)
    _store(request).create_work_order(public_id, staff["pk"], payload.team, payload.description, payload.dueAt, payload.assignee, _ip(request))
    return case_view(request, response, public_id, staff)


@router.post("/work-orders/{work_order_id}/transitions", response_model=StaffCase)
def change_work_order(work_order_id: str, payload: WorkOrderChangeIn, request: Request, response: Response,
                      staff: dict = Depends(current_staff)) -> dict:
    _check_order_id(work_order_id)
    public_id = _store(request).update_work_order(work_order_id, staff["pk"], payload.to, payload.expectedStatus, payload.note, _ip(request))
    return case_view(request, response, public_id, staff)


@router.get("/work-orders", response_model=WorkOrderList)
def list_work_orders(request: Request, response: Response, staff: dict = Depends(current_staff),
                     status: str | None = Query(None, pattern="^(Open|In Progress|Completed|Cancelled)$"),
                     overdue: bool = False, department: str | None = Query(None, pattern=r"^[a-z0-9][a-z0-9-]{1,31}$"),
                     limit: int = Query(50, ge=1, le=STAFF_LIST_MAX), offset: int = Query(0, ge=0)) -> dict:
    items, total = _store(request).list_work_orders(staff["pk"], status, overdue, department, limit, offset)
    response.headers["Cache-Control"] = "no-store"
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/work-orders/{work_order_id}/evidence", status_code=201, response_model=RepairEvidenceReceipt)
async def upload_repair_evidence(work_order_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    """After-repair photo for a work order, through the same streaming checks as citizen evidence
    (signature sniffing, size limit, SHA-256, quota). Authorised before the body is read."""
    store = _store(request)
    storage: EvidenceStorage = request.app.state.evidence_storage
    settings: UploadSettings = request.app.state.upload_settings
    response.headers["Cache-Control"] = "no-store"
    enforce(request, "evidence_per_ip", "ip")
    _check_order_id(work_order_id)
    await run_in_threadpool(store.work_order_context, work_order_id, staff["pk"])
    declared = request.headers.get("content-length", "")
    declared_size = int(declared) if declared.isdigit() else None
    if declared_size is not None and declared_size > settings.max_request_bytes:
        raise ApiProblem(413, "payload_too_large", "The upload is larger than the allowed size.")
    quota: EvidenceQuota = request.app.state.evidence_quota
    with quota.reserve(declared_size or settings.max_request_bytes) as reservation:
        reader, captured_at = await receive_upload(request, storage, settings)
        pending, kind = reader.pending, reader.kind
        try:
            if kind.media_type != "photo":
                raise ApiProblem(422, "media_type_mismatch", "After-repair evidence must be a JPEG photo.")
            key = await run_in_threadpool(pending.commit)
        except BaseException:
            pending.abort()
            raise
        meta = {"evidenceId": "ev_" + secrets.token_urlsafe(16), "storageKey": key, "mediaType": kind.media_type,
                "contentType": kind.content_type, "byteSize": pending.size, "sha256": pending.sha256,
                "capturedAt": captured_at, "uploadedAt": utc_now(), "storageBackend": storage.name}
        try:
            row = await run_in_threadpool(store.add_repair_evidence, work_order_id, staff["pk"], meta, _ip(request))
        except BaseException:
            await run_in_threadpool(storage.delete, key)  # no file without a row, and no row without a file
            raise
        reservation.commit(pending.size)
        return {k: row[k] for k in ("evidenceId", "mediaType", "contentType", "byteSize", "sha256", "capturedAt", "uploadedAt")}


@router.get("/work-orders/{work_order_id}/evidence/{evidence_id}")
def view_work_order_evidence(work_order_id: str, evidence_id: str, request: Request, staff: dict = Depends(current_staff)):
    """Stream a before/after file of a work order. Each view is written to the evidence access log first."""
    _check_order_id(work_order_id)
    if not EVIDENCE_ID.fullmatch(evidence_id):
        raise ApiProblem(404, "evidence_not_found", "No such evidence for this work order.")
    evidence = _store(request).work_order_evidence(work_order_id, evidence_id, staff["pk"])
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", "No such evidence for this work order.")
    return stream_evidence(request, evidence, staff, f"{work_order_id}-{evidence['role']}")


@router.get("/complaints/{public_id}/audit", response_model=list[AuditEntry])
def audit_trail(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    _check_id(public_id)
    entries = _store(request).audit_trail(public_id, staff["pk"])
    if entries is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    response.headers["Cache-Control"] = "no-store"
    return entries


@router.get("/departments", response_model=list[DepartmentOut])
def departments(request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    return _store(request).list_departments()
