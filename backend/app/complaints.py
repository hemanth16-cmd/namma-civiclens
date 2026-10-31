"""Complaint endpoints: anonymous creation, public tracking, and upload-token renewal.

None of them has user authentication. Each is rate-limited per client IP (and token renewal also
per complaint) before any expensive work; see ratelimit.py. Request bodies are never logged, and
the claim code appears only in a creation receipt.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response

from .claims import verify_claim_code
from .errors import ApiProblem
from .models import (CATEGORIES, CitizenResponseIn, CitizenResponseOut, ComplaintCreate, ComplaintReceipt, PublicComplaint, PublicComplaintList,
                     UploadTokenRequest, UploadTokenResponse)
from .ratelimit import client_ip, enforce
from .store import ComplaintStore, IdempotencyCompleted, IdempotencyConflict, iso

router = APIRouter(prefix="/complaints", tags=["complaints"])

# Accepts generated IDs (NCL-2026-K7Q2MX) and the seeded demo format (NCL-2026-DEMO01).
PUBLIC_ID_PATTERN = re.compile(r"^NCL-\d{4}-[A-Z0-9]{6}$")
NOT_FOUND = "No complaint was found with that ID."
# Clients should send at least 128 random bits, URL-safe (the frontend sends 256).
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


def _store(request: Request) -> ComplaintStore:
    return request.app.state.store


def _limit_create(request: Request) -> None:
    enforce(request, "create_per_ip", "ip")


def _limit_track(request: Request) -> None:
    enforce(request, "track_per_ip", "ip")


def request_fingerprint(data: dict) -> str:
    """SHA-256 of the normalised request: the same submission always gives the same fingerprint."""
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@router.post("", status_code=201, response_model=ComplaintReceipt, dependencies=[Depends(_limit_create)])
def create_complaint(payload: ComplaintCreate, request: Request, response: Response, background: BackgroundTasks) -> dict:
    data = payload.model_dump()
    if payload.capturedAt is not None:
        data["capturedAt"] = iso(payload.capturedAt)
    if payload.gps.timestamp is not None:
        data["gps"]["timestamp"] = iso(datetime.fromtimestamp(payload.gps.timestamp / 1000, timezone.utc))
    store = _store(request)
    response.headers["Cache-Control"] = "no-store"

    key = request.headers.get("idempotency-key")
    if key is None:
        record, claim = store.create(data)
        replayed = False
    else:
        if not IDEMPOTENCY_KEY_PATTERN.fullmatch(key):
            raise ApiProblem(400, "invalid_idempotency_key", "Idempotency-Key must be 22–128 URL-safe characters (at least 128 random bits).")
        key_hash = hashlib.sha256(key.encode("ascii")).hexdigest()
        try:
            outcome, record, claim = store.create_idempotent(data, key_hash, request_fingerprint(data), request.app.state.replay_window)
        except IdempotencyConflict:
            raise ApiProblem(409, "idempotency_key_conflict", "This Idempotency-Key was already used for a different request.")
        except IdempotencyCompleted as done:
            raise ApiProblem(409, "idempotency_key_used",
                             "This submission was already completed and its claim code was delivered earlier. For security it cannot be shown again.",
                             extra={"complaintId": done.public_id})
        replayed = outcome == "replayed"
        if replayed:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
    if not replayed:  # staff are told once, after the response (see notifications.py)
        background.add_task(request.app.state.notifier.complaint_created, record["id"], record["category"])
    response.headers["Location"] = f"{request.url.path}/{record['id']}"
    return {**record, "claim": claim, "replayed": replayed}


@router.get("", response_model=PublicComplaintList, dependencies=[Depends(_limit_track)])
def list_community_complaints(request: Request, response: Response,
                              category: str | None = Query(None, max_length=60), area: str | None = Query(None, max_length=120),
                              status: str | None = Query(None, pattern="^(Submitted|Under Review|Assigned|In Progress|Marked Resolved)$"),
                              limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    """Community issues across Bengaluru: every complaint in the service area, newest first, with the
    public fields only, optionally filtered by issue type (category), neighbourhood (area) and status.
    Facets give the counts behind the filter menus."""
    if category is not None and category not in CATEGORIES:
        raise ApiProblem(422, "validation_error", "Choose a valid issue category.")
    response.headers["Cache-Control"] = "no-store"
    return _store(request).list_public_complaints(category or None, area or None, status or None, limit, offset)


@router.get("/{public_id}", response_model=PublicComplaint, dependencies=[Depends(_limit_track)])
def get_public_complaint(public_id: str, request: Request) -> dict:
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    record = _store(request).get(public_id)
    if record is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return record


@router.post("/{public_id}/upload-token", response_model=UploadTokenResponse)
def renew_upload_token(public_id: str, payload: UploadTokenRequest, request: Request, response: Response) -> dict:
    """Exchange the private claim code (in the JSON body, never the URL) for a fresh upload token,
    for example after the first token expired. Replaces any earlier token. Rate limits are checked
    before the database lookup and the scrypt verification."""
    enforce(request, "token_per_ip", "ip")
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    enforce(request, "token_per_complaint", public_id)
    store = _store(request)
    record = store.get(public_id)
    if record is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if not verify_claim_code(payload.claim, record["claimHash"]):
        raise ApiProblem(401, "unauthorized", "The claim code does not match this complaint.")
    issued = store.issue_upload_token(public_id)
    if issued is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    response.headers["Cache-Control"] = "no-store"
    return {"uploadToken": issued[0], "uploadTokenExpiresAt": issued[1]}


@router.post("/{public_id}/citizen-responses", response_model=CitizenResponseOut)
def citizen_response(public_id: str, payload: CitizenResponseIn, request: Request, response: Response) -> dict:
    """A citizen's view of a resolution: Appears Resolved, Still Present or Cannot Confirm. Proven
    with the private claim code in the JSON body, never the URL. Changes only the verification state,
    under the rules in docs/api-contract.md §6; the operational status never changes. Rate-limited
    per IP and per complaint before the database lookup and the scrypt check."""
    enforce(request, "citizen_response_per_ip", "ip")
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    enforce(request, "citizen_response_per_complaint", public_id)
    store = _store(request)
    record = store.get(public_id)
    if record is None or not verify_claim_code(payload.claim, record["claimHash"]):
        # One message for both cases, so the endpoint cannot be used to discover which IDs exist.
        raise ApiProblem(401, "unauthorized", "The complaint ID and claim code do not match.")
    response.headers["Cache-Control"] = "no-store"
    return store.citizen_response(public_id, payload.response, client_ip(request, request.app.state.rate_limit_settings.trusted_proxies))
