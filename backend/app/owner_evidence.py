"""Owner access to a complaint's original evidence, through the same retrieval service as staff
(evidence_access.open_and_stream): same evidence metadata, storage backend selection and streaming.

Two ways to prove ownership, both separate from staff authorisation:
- POST /complaints/{id}/evidence/view {"claim"}: the private claim code from the receipt, sent in the
  JSON body (never in the URL). Works for a passerby without an account.
- GET /residents/me/complaints/{id}/evidence: a resident session whose account owns the complaint.
- GET /residents/me/complaints/{id}/evidence/{evidenceId}: one file (the original report or an after-repair
  photo) of a complaint the resident owns; My Complaints uses it (views/my-complaints.js).

The browser fetches the bytes and shows them from a blob: URL, so no credential or storage URL is ever
put in a link. Both routes are rate-limited before the claim-code or ownership check, and every view is
written to audit_log (who: "citizen", how: claim code or resident account). A view by the owner does not
send the "evidence reviewed" notification that a staff view sends.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .auth import _DUMMY_HASH
from .cases_api import EVIDENCE_ID
from .claims import verify_claim_code
from .complaints import PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .evidence_access import open_and_stream
from .models import UploadTokenRequest
from .ratelimit import client_ip, enforce
from .resident_auth import current_resident

router = APIRouter(tags=["owner evidence"])

BAD_CLAIM = "The complaint ID or claim code is not correct. Both are on the receipt shown after the report was submitted."
NO_EVIDENCE = "No photo or video has been uploaded for this complaint yet."


def _stream(request: Request, public_id: str, complaint_pk: int, via: str, resident_id: str | None) -> StreamingResponse:
    store = request.app.state.store
    evidence = store.get_evidence(complaint_pk, "original")
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", NO_EVIDENCE)
    ip = client_ip(request, request.app.state.rate_limit_settings.trusted_proxies)
    return open_and_stream(request, evidence, f"{public_id}-evidence",
                           lambda: store.log_owner_evidence_view(complaint_pk, evidence["evidenceId"], via, resident_id, ip))


@router.post("/complaints/{public_id}/evidence/view")
def view_with_claim_code(public_id: str, payload: UploadTokenRequest, request: Request) -> StreamingResponse:
    """The original photo or video, for whoever holds the complaint's private claim code."""
    enforce(request, "owner_evidence_per_ip", "ip")
    public_id = public_id.strip().upper()
    ref = request.app.state.store.complaint_ref(public_id) if PUBLIC_ID_PATTERN.fullmatch(public_id) else None
    if ref is not None:
        enforce(request, "owner_evidence_per_complaint", public_id)
    # Always one scrypt check, so an unknown ID takes as long as a wrong code.
    if not verify_claim_code(payload.claim.strip(), ref["claimHash"] if ref else _DUMMY_HASH) or ref is None:
        raise ApiProblem(403, "invalid_claim", BAD_CLAIM)
    return _stream(request, public_id, ref["pk"], "claim_code", None)


@router.get("/residents/me/complaints/{public_id}/evidence")
def view_own_evidence(public_id: str, request: Request, resident: dict = Depends(current_resident)) -> StreamingResponse:
    """The original photo or video of a complaint in the signed-in resident's account. Any other ID is 404."""
    enforce(request, "owner_evidence_per_ip", "ip")
    store = request.app.state.store
    ref = store.complaint_ref(public_id) if PUBLIC_ID_PATTERN.fullmatch(public_id) else None
    if ref is None or store.owner_of(public_id) != resident["pk"]:
        raise ApiProblem(404, "not_found", "None of your complaints has that ID.")
    return _stream(request, public_id, ref["pk"], "resident", resident["residentId"])


@router.get("/residents/me/complaints/{public_id}/evidence/{evidence_id}")
def view_own_evidence_file(public_id: str, evidence_id: str, request: Request, resident: dict = Depends(current_resident)) -> StreamingResponse:
    """One evidence file (original or after-repair) of a complaint the signed-in resident owns. Any other
    complaint or file is 404, whether or not it exists. Rate-limited and audited like the other owner routes."""
    enforce(request, "owner_evidence_per_ip", "ip")
    store = request.app.state.store
    evidence = None
    if PUBLIC_ID_PATTERN.fullmatch(public_id) and EVIDENCE_ID.fullmatch(evidence_id):
        evidence = store.resident_evidence(resident["pk"], public_id, evidence_id)
    if evidence is None:
        raise ApiProblem(404, "evidence_not_found", "None of your complaints has that evidence file.")
    ip = client_ip(request, request.app.state.rate_limit_settings.trusted_proxies)
    return open_and_stream(request, evidence, f"{public_id}-{evidence['role']}",
                           lambda: store.log_owner_evidence_view(evidence["complaintPk"], evidence["evidenceId"], "resident",
                                                                resident["residentId"], ip))
