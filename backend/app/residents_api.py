"""Resident accounts API (/api/v1/residents): registration, sign-in and sign-out, profile, the
resident's own complaints, claiming an anonymous complaint with its private claim code, and preferences.

Every endpoint below /residents/me needs a resident bearer token (resident_auth.current_resident).
Staff tokens are not accepted here, and resident tokens are not accepted on /staff. A resident sees
only complaints they own; there is no endpoint that reveals another resident's details, and public
tracking (GET /complaints/{id}) never shows who owns a complaint.
"""

from fastapi import APIRouter, Depends, Request, Response

from .auth import _DUMMY_HASH
from .claims import verify_claim_code
from .complaints import PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .models import (ClaimRequest, ClaimResult, ResidentComplaint, ResidentComplaintList, ResidentIdentity, ResidentPreferences,
                     ResidentProfileUpdate, ResidentRegister, ResidentSessionOut, ResidentSignIn)
from .ratelimit import client_ip, enforce
from .resident_auth import (current_resident, email_problem, hash_password, new_session, normalise_email, password_problem,
                            resident_identity, sign_in)
from .cases_api import EVIDENCE_ID
from .staff_api import stream_evidence
from .store import ResidentExists

router = APIRouter(prefix="/residents", tags=["residents"])

BAD_CLAIM = "The complaint ID or claim code is not correct. Both are on the receipt shown after the report was submitted."


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.post("", status_code=201, response_model=ResidentSessionOut)
def register(payload: ResidentRegister, request: Request, response: Response) -> dict:
    """Create a resident account and sign it in. Anonymous reporting never needs this."""
    enforce(request, "resident_register_per_ip", "ip")
    email = normalise_email(payload.email)
    problem = email_problem(email) or password_problem(payload.password)
    if problem:
        raise ApiProblem(422, "validation_error", problem)
    store = request.app.state.store
    try:
        account = store.create_resident(email, payload.displayName.strip(), hash_password(payload.password))
    except ResidentExists:
        raise ApiProblem(409, "email_taken", "An account with this email address already exists. Sign in instead.")
    _no_store(response)
    return new_session(store, account, request.app.state.resident_session_ttl)


@router.post("/sessions", response_model=ResidentSessionOut)
def create_session(payload: ResidentSignIn, request: Request, response: Response) -> dict:
    """Sign in. Rate-limited per IP and per account before the password check."""
    enforce(request, "resident_login_per_ip", "ip")
    enforce(request, "resident_login_per_account", "resident:" + normalise_email(payload.email))
    session = sign_in(request.app.state.store, payload.email, payload.password, request.app.state.resident_session_ttl)
    if session is None:
        raise ApiProblem(401, "unauthorized", "The email address or password is incorrect.")
    _no_store(response)
    return session


@router.delete("/sessions/current", status_code=204)
def end_session(request: Request, resident: dict = Depends(current_resident)) -> Response:
    request.app.state.store.revoke_resident_session(resident["tokenHash"])
    return Response(status_code=204)


@router.get("/me", response_model=ResidentIdentity)
def me(response: Response, resident: dict = Depends(current_resident)) -> dict:
    _no_store(response)
    return {k: resident[k] for k in ("residentId", "email", "displayName", "demo", "createdAt")}


@router.patch("/me", response_model=ResidentIdentity)
def update_me(payload: ResidentProfileUpdate, request: Request, response: Response, resident: dict = Depends(current_resident)) -> dict:
    store = request.app.state.store
    store.update_resident(resident["email"], display_name=payload.displayName.strip())
    _no_store(response)
    return resident_identity(store.get_resident(resident["email"]))


@router.get("/me/complaints", response_model=ResidentComplaintList)
def my_complaints(request: Request, response: Response, limit: int = 100, offset: int = 0,
                  resident: dict = Depends(current_resident)) -> dict:
    items, total = request.app.state.store.resident_complaints(resident["pk"], limit, offset)
    _no_store(response)
    return {"items": items, "total": total, "limit": max(1, min(limit, 100)), "offset": max(0, offset)}


@router.get("/me/complaints/{public_id}", response_model=ResidentComplaint)
def my_complaint(public_id: str, request: Request, response: Response, resident: dict = Depends(current_resident)) -> dict:
    """Only a complaint this resident owns; any other ID is 404, whether or not it exists."""
    record = request.app.state.store.resident_complaint(resident["pk"], public_id) if PUBLIC_ID_PATTERN.fullmatch(public_id) else None
    if record is None:
        raise ApiProblem(404, "not_found", "None of your complaints has that ID.")
    _no_store(response)
    return record


@router.get("/me/complaints/{public_id}/evidence/{evidence_id}")
def my_evidence(public_id: str, evidence_id: str, request: Request, resident: dict = Depends(current_resident)):
    """Stream a photo or video of a complaint this resident owns (the original report or an after-repair
    photo), through the API with the resident session: the bucket stays private and no URL is issued.
    Any other complaint or file is 404, whether or not it exists."""
    evidence = None
    if PUBLIC_ID_PATTERN.fullmatch(public_id) and EVIDENCE_ID.fullmatch(evidence_id):
        evidence = request.app.state.store.resident_evidence(resident["pk"], public_id, evidence_id)
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", "None of your complaints has that evidence file.")
    return stream_evidence(request, evidence, None, f"{public_id}-{evidence['role']}")


@router.post("/me/claims", response_model=ClaimResult)
def claim(payload: ClaimRequest, request: Request, response: Response, resident: dict = Depends(current_resident)) -> dict:
    """Link an anonymous complaint to this account. Needs the complaint's public ID AND its private claim
    code from the receipt. A wrong ID or code gives one generic answer (no hint which was wrong)."""
    enforce(request, "resident_claim_per_ip", "ip")
    public_id = payload.id.strip().upper()
    store = request.app.state.store
    ref = store.complaint_ref(public_id) if PUBLIC_ID_PATTERN.fullmatch(public_id) else None
    if ref is not None:
        enforce(request, "resident_claim_per_complaint", public_id)
    # Always one scrypt check, so an unknown ID takes as long as a wrong code.
    valid = verify_claim_code(payload.claim.strip(), ref["claimHash"] if ref else _DUMMY_HASH)
    if ref is None or not valid:
        raise ApiProblem(403, "invalid_claim", BAD_CLAIM)
    outcome = store.claim_complaint(public_id, resident, client_ip(request, request.app.state.rate_limit_settings.trusted_proxies))
    _no_store(response)
    return {"outcome": outcome, "complaint": store.resident_complaint(resident["pk"], public_id)}


@router.get("/me/preferences", response_model=ResidentPreferences)
def get_preferences(request: Request, response: Response, resident: dict = Depends(current_resident)) -> dict:
    _no_store(response)
    return ResidentPreferences(**request.app.state.store.get_resident_preferences(resident["pk"])).model_dump()


@router.put("/me/preferences", response_model=ResidentPreferences)
def put_preferences(payload: ResidentPreferences, request: Request, response: Response, resident: dict = Depends(current_resident)) -> dict:
    _no_store(response)
    return request.app.state.store.set_resident_preferences(resident["pk"], payload.model_dump())
