"""Supervisor operations API: ward/zone scope, the city → zone → ward dashboard, multi-ward complaint
lists, bulk allocation and manual ward assignment. Every route needs a staff bearer session; the role
(supervisor or admin) and the geographic scope are checked by store.py against current database rows
inside the request's transaction, so an official gets 403 and nobody can widen their own scope (scope
is granted only by the CLI, python -m app.staff scope ...).

Ward assignment is boundary-based only (wards.py): without an imported boundary polygon a complaint's
ward stays "unverified" until a supervisor sets it here.
"""

from fastapi import APIRouter, Depends, Query, Request, Response

from .auth import current_staff
from .cases_api import _check_id, _ip, _store, case_view
from .complaints import NOT_FOUND
from .errors import ApiProblem
from .models import (CATEGORIES, AllocationEntry, AllocationIn, AllocationOut, AssignableStaff, StaffCase, SupervisorComplaintList,
                     SupervisorDashboard, SupervisorScope, WardSetIn)
from .store import STAFF_LIST_MAX

router = APIRouter(prefix="/staff", tags=["supervisor operations"])
UNRESOLVED = "^(Submitted|Under Review|Assigned|In Progress)$"
MAX_WARDS = 300


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _ward_list(raw: str | None) -> list[int]:
    """`wards=12,15,40` → [12, 15, 40]. Ward numbers only; names are never matched."""
    if not raw:
        return []
    try:
        numbers = sorted({int(part) for part in raw.split(",") if part.strip()})
    except ValueError:
        raise ApiProblem(422, "invalid_wards", "Wards must be a comma-separated list of ward numbers.") from None
    if len(numbers) > MAX_WARDS or any(n < 1 or n > 9999 for n in numbers):
        raise ApiProblem(422, "invalid_wards", "Choose up to 300 valid ward numbers.")
    return numbers


@router.get("/supervisor/scope", response_model=SupervisorScope)
def scope(request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    _no_store(response)
    return _store(request).supervisor_scope(staff["pk"])


@router.get("/supervisor/dashboard", response_model=SupervisorDashboard)
def dashboard(request: Request, response: Response, staff: dict = Depends(current_staff),
              zone: int | None = Query(None, ge=1, le=9999), ward: int | None = Query(None, ge=1, le=9999)) -> dict:
    _no_store(response)
    return _store(request).supervisor_dashboard(staff["pk"], zone, ward)


@router.get("/supervisor/complaints", response_model=SupervisorComplaintList)
def complaints(request: Request, response: Response, staff: dict = Depends(current_staff),
               wards: str | None = Query(None, max_length=2000), category: str | None = Query(None, max_length=60),
               status: str | None = Query(None, pattern=UNRESOLVED), minAgeDays: int | None = Query(None, ge=0, le=3650),
               maxAgeDays: int | None = Query(None, ge=0, le=3650), unverifiedWard: bool = False,
               limit: int = Query(50, ge=1, le=STAFF_LIST_MAX), offset: int = Query(0, ge=0)) -> dict:
    if category is not None and category not in CATEGORIES:
        raise ApiProblem(422, "invalid_category", "Choose a valid category.")
    _no_store(response)
    return _store(request).supervisor_complaints(staff["pk"], _ward_list(wards), category, status, minAgeDays, maxAgeDays,
                                                 unverifiedWard, limit, offset)


@router.get("/supervisor/staff", response_model=list[AssignableStaff])
def assignable_staff(request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    _no_store(response)
    return _store(request).assignable_staff(staff["pk"])


@router.post("/supervisor/allocations", response_model=AllocationOut)
def allocate(payload: AllocationIn, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    _no_store(response)
    return _store(request).allocate(staff["pk"], payload.complaints, payload.department, payload.assignee, payload.priority,
                                    payload.dueAt, payload.reason, payload.note, _ip(request))


@router.get("/complaints/{public_id}/allocations", response_model=list[AllocationEntry], response_model_by_alias=True)
def allocations(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> list[dict]:
    _check_id(public_id)
    entries = _store(request).allocation_history(staff["pk"], public_id)
    if entries is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    _no_store(response)
    return entries


@router.post("/complaints/{public_id}/ward", response_model=StaffCase)
def set_ward(public_id: str, payload: WardSetIn, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    """Manual ward by a supervisor or admin (audited; the original GPS is kept)."""
    _check_id(public_id)
    _store(request).set_complaint_ward(public_id, staff["pk"], payload.ward, payload.reason, _ip(request))
    return case_view(request, response, public_id, staff)
