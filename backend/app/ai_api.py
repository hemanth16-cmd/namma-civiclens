"""AI analysis API for signed-in staff. Every route needs a staff bearer session, and officials see
only complaints in their scope (lifecycle.in_scope); reviews need a supervisor or admin.

    GET  /staff/complaints/{id}/ai                                   AI results for the complaint's photos
    POST /staff/complaints/{id}/ai/retry                             re-run failed or missing analyses (202)
    POST /staff/complaints/{id}/verify-repair                        compare the original and a repair photo (202)
    GET  /staff/complaints/{id}/repair-assessment                    before/after assessments, newest first
    POST /staff/complaints/{id}/repair-assessment/{aid}/review       supervisor approves or rejects a suggestion

Results are suggestions. None of these routes changes a complaint's status, its citizen verification or
any work order. Responses never contain storage keys, API keys, provider responses or image coordinates
beyond the detections' pixel boxes.
"""

import re
from typing import Literal

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from .auth import current_staff
from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .ratelimit import client_ip

router = APIRouter(prefix="/staff", tags=["ai analysis"])
public_router = APIRouter(tags=["ai analysis"])
EVIDENCE_ID = re.compile(r"^ev_[A-Za-z0-9_-]{22}$")
ASSESSMENT_ID = re.compile(r"^ra_[A-Za-z0-9_-]{22}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class VerifyRepairIn(_Strict):
    repairEvidenceId: str | None = Field(None, description="A repair photo of this complaint; default: the latest one")


class ReviewIn(_Strict):
    decision: Literal["approved", "rejected"]
    note: str = Field("", max_length=1000)


class AiStatus(BaseModel):
    enabled: bool
    provider: str | None
    reason: str | None
    disclosure: str | None
    thresholds: dict


class AiInference(BaseModel):
    id: str
    evidenceId: str
    evidenceRole: str
    detector: str
    provider: str
    mechanism: str
    modelId: str
    modelVersion: str | None
    classPrompt: str | None
    state: str
    originalClass: str | None
    detectedCategory: str | None
    topConfidence: float | None
    detectionCount: int
    detections: list[dict]
    confidence: dict
    imageWidth: int | None
    imageHeight: int | None
    errorCode: str | None
    attempts: int
    createdAt: str
    startedAt: str | None
    completedAt: str | None


class AiAnalysis(BaseModel):
    complaintId: str
    category: str
    ai: AiStatus
    routing: dict
    inferences: list[AiInference]
    reset: int | None = None


class RepairAssessment(BaseModel):
    id: str
    workOrderId: str | None
    originalEvidenceId: str
    repairEvidenceId: str
    state: str
    suggestedOutcome: Literal["likely_repaired", "damage_visible", "inconclusive"] | None
    verificationConfidence: float | None
    locationCheck: Literal["match", "mismatch", "unverified"] | None
    gpsDistanceM: float | None
    details: dict
    errorCode: str | None
    attempts: int
    requestedBy: str | None
    review: dict
    verifiedResolution: Literal["verified_repaired", "verified_not_repaired", "unverified"]
    createdAt: str
    completedAt: str | None
    updatedAt: str


class RepairAssessments(BaseModel):
    complaintId: str
    ai: AiStatus
    assessments: list[RepairAssessment]


def _ai(request: Request):
    return request.app.state.ai


def _ip(request: Request) -> str:
    return client_ip(request, request.app.state.rate_limit_settings.trusted_proxies)


def _complaint(request: Request, public_id: str, staff: dict) -> dict:
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    complaint = _ai(request).db.complaint_for_staff(public_id, staff["pk"])  # RuleViolation 403 outside scope
    if complaint is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    return complaint


def _analysis(request: Request, complaint: dict) -> dict:
    ai = _ai(request)
    return {"complaintId": complaint["id"], "category": complaint["category"], "ai": ai.status(),
            "routing": ai.routing(complaint["category"]), "inferences": ai.db.results_for_complaint(complaint["pk"])}


def _assessments(request: Request, complaint: dict) -> dict:
    ai = _ai(request)
    return {"complaintId": complaint["id"], "ai": ai.status(), "assessments": ai.db.assessments_for_complaint(complaint["pk"])}


@router.get("/complaints/{public_id}/ai", response_model=AiAnalysis)
def get_analysis(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    complaint = _complaint(request, public_id, staff)
    response.headers["Cache-Control"] = "no-store"
    return _analysis(request, complaint)


@router.post("/complaints/{public_id}/ai/retry", status_code=202, response_model=AiAnalysis)
async def retry_analysis(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    complaint = await run_in_threadpool(_complaint, request, public_id, staff)
    reset = await run_in_threadpool(_ai(request).retry, complaint, staff["pk"], _ip(request))
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_analysis, request, complaint) | {"reset": reset}


@router.post("/complaints/{public_id}/verify-repair", status_code=202, response_model=RepairAssessments)
async def verify_repair(public_id: str, request: Request, response: Response, payload: VerifyRepairIn | None = None,
                        staff: dict = Depends(current_staff)) -> dict:
    evidence_id = payload.repairEvidenceId if payload else None
    if evidence_id is not None and not EVIDENCE_ID.fullmatch(evidence_id):
        raise ApiProblem(404, "no_repair_evidence", "No after-repair photo with that ID belongs to this complaint.")
    complaint = await run_in_threadpool(_complaint, request, public_id, staff)
    await run_in_threadpool(_ai(request).request_repair_assessment, complaint, evidence_id, staff["pk"], _ip(request))
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(_assessments, request, complaint)


@router.get("/complaints/{public_id}/repair-assessment", response_model=RepairAssessments)
def get_assessments(public_id: str, request: Request, response: Response, staff: dict = Depends(current_staff)) -> dict:
    complaint = _complaint(request, public_id, staff)
    response.headers["Cache-Control"] = "no-store"
    return _assessments(request, complaint)


@router.post("/complaints/{public_id}/repair-assessment/{assessment_id}/review", response_model=RepairAssessment)
def review_assessment(public_id: str, assessment_id: str, payload: ReviewIn, request: Request, response: Response,
                      staff: dict = Depends(current_staff)) -> dict:
    complaint = _complaint(request, public_id, staff)
    if not ASSESSMENT_ID.fullmatch(assessment_id):
        raise ApiProblem(404, "not_found", "No repair assessment was found with that ID for this complaint.")
    ai = _ai(request)
    ai.db.review_assessment(complaint["id"], assessment_id, staff["pk"], payload.decision, payload.note, _ip(request))
    response.headers["Cache-Control"] = "no-store"
    return ai.db.assessment(assessment_id)


class AiDisclosure(BaseModel):
    enabled: bool
    disclosure: str | None


@public_router.get("/ai", response_model=AiDisclosure)
def ai_disclosure(request: Request) -> dict:
    """Public: whether uploaded photos may be analysed by an external AI service, and the notice to show
    citizens before they upload. Contains no configuration details."""
    status = _ai(request).status()
    return {"enabled": status["enabled"], "disclosure": status["disclosure"]}
