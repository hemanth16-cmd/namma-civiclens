"""Request and response schemas for the complaint API.

Field names are camelCase to match dist/services.js, so the browser data contract
does not change when the frontend is connected later.
"""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, field_validator

# Exactly the categories in dist/services.js.
CATEGORIES = (
    "Potholes", "Road cracks and damage", "Garbage accumulation", "Illegal dumping",
    "Drainage or waterlogging", "Broken streetlights", "Damaged footpaths", "Water leakage", "Other civic issues",
)
Category = Literal[CATEGORIES]

# Allow for small clock differences between a phone and the server.
MAX_CLOCK_SKEW = timedelta(minutes=10)
# Earliest accepted GPS fix time (2000-01-01T00:00:00Z) in epoch milliseconds.
MIN_FIX_EPOCH_MS = 946_684_800_000

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
ShortText = Annotated[str, Field(max_length=120)]


def _not_in_future(dt: datetime) -> datetime:
    if dt > datetime.now(timezone.utc) + MAX_CLOCK_SKEW:
        raise ValueError("must not be in the future")
    return dt


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GpsIn(_Strict):
    """Location evidence as the capture screen builds it (location-service.js toFix(),
    exif.js, plus fixAgeSeconds and the optional address lookup)."""

    lat: Annotated[FiniteFloat, Field(ge=-90, le=90)]
    lng: Annotated[FiniteFloat, Field(ge=-180, le=180)]
    accuracy: Annotated[FiniteFloat, Field(ge=0)] | None = None
    sourceType: Literal["gps", "exif", "manual"]
    source: Annotated[str, Field(max_length=80)] | None = None
    # Browser fix time in epoch milliseconds (Geolocation API). Stored as ISO 8601.
    timestamp: Annotated[FiniteFloat, Field(ge=MIN_FIX_EPOCH_MS)] | None = None
    fixAgeSeconds: Annotated[int, Field(ge=0, le=86_400)] | None = None
    neighbourhood: ShortText | None = None
    address: Annotated[str, Field(max_length=300)] | None = None

    @field_validator("timestamp")
    @classmethod
    def _fix_time_not_in_future(cls, v: float | None) -> float | None:
        if v is not None:
            _not_in_future(datetime.fromtimestamp(v / 1000, timezone.utc))
        return v


class ComplaintCreate(_Strict):
    """Complaint metadata only. Media upload is a separate, later endpoint.

    Not accepted from the client: `owner` (no authentication yet, so an owner name
    sent by the client cannot be trusted) and `uploadedAt` (set by the server).
    """

    category: Category
    mediaType: Literal["photo", "video"] = "photo"
    description: Annotated[str, Field(max_length=1000)] = ""
    shareEvidence: bool = False
    neighbourhood: ShortText | None = None
    gps: GpsIn
    # services.js receives this as `created`; both names are accepted.
    capturedAt: AwareDatetime | None = Field(default=None, validation_alias=AliasChoices("capturedAt", "created"))
    captureSource: Annotated[str, Field(max_length=80)] | None = None

    @field_validator("capturedAt")
    @classmethod
    def _captured_not_in_future(cls, v: datetime | None) -> datetime | None:
        return _not_in_future(v) if v is not None else v


class TimelineEntry(BaseModel):
    status: str
    text: str
    date: str


class VerificationEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    date: str
    type: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    response: str | None = None
    by: str
    actor: str
    note: str = ""


class PublicComplaint(BaseModel):
    """Public tracking projection: the same allowlist as api.publicRecord() in dist/services.js.
    No claim code or hash, owner, precise GPS, media, description or internal fields."""

    id: str
    category: str
    area: str
    status: str
    verification: str
    department: str
    updated: str
    timeline: list[TimelineEntry]
    verificationLog: list[VerificationEntry]
    # Public ID of the complaint this report was merged into as a duplicate (schema 8), else null.
    mergedInto: str | None = None


class ReceiptGps(BaseModel):
    lat: float
    lng: float
    accuracy: float | None
    source: str | None
    sourceType: str
    timestamp: str | None
    address: str | None


class ComplaintReceipt(BaseModel):
    """Returned once, to the submitter, by POST /complaints. The only response that
    contains the plaintext claim code. Mirrors api.receipt() in dist/services.js."""

    id: str
    claim: str
    category: str
    area: str
    created: str
    capturedAt: str | None
    uploadedAt: str
    captureSource: str | None
    gps: ReceiptGps
    mediaType: str
    shareEvidence: bool
    status: str
    verification: str
    # Short-lived bearer token for POST /complaints/{id}/evidence. Shown only here and by the
    # upload-token endpoint; only its hash is stored.
    uploadToken: str
    uploadTokenExpiresAt: str
    # True when this is an idempotent replay: the same complaint, with a newly issued claim code and
    # upload token (any earlier ones for this complaint no longer work).
    replayed: bool = False


class UploadTokenRequest(_Strict):
    claim: Annotated[str, Field(min_length=1, max_length=100)]


class UploadTokenResponse(BaseModel):
    uploadToken: str
    uploadTokenExpiresAt: str


class StaffSignIn(_Strict):
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class StaffIdentity(BaseModel):
    username: str
    displayName: str
    role: str


class StaffSessionOut(BaseModel):
    token: str
    expiresAt: str
    staff: StaffIdentity


class StaffEvidenceSummary(BaseModel):
    """Evidence metadata for staff. Never a storage key, path or URL: the file is fetched through
    GET /staff/complaints/{id}/evidence with the staff session."""
    evidenceId: str
    mediaType: str
    contentType: str
    byteSize: int
    sha256: str
    capturedAt: str | None
    uploadedAt: str


class StaffGps(BaseModel):
    lat: float
    lng: float
    accuracy: float | None
    source: str | None
    sourceType: str
    timestamp: str | None
    address: str | None


class StaffListGps(BaseModel):
    """Location for the staff map. Staff may see precise coordinates (as in StaffComplaint)."""
    lat: float
    lng: float
    accuracy: float | None
    sourceType: str


class StaffComplaintSummary(BaseModel):
    id: str
    title: str
    category: str
    area: str
    status: str
    verification: str
    department: str
    created: str
    updated: str
    mediaType: str
    hasEvidence: bool
    gps: StaffListGps
    mergedInto: str | None = None


class StaffComplaintList(BaseModel):
    items: list[StaffComplaintSummary]
    total: int
    limit: int
    offset: int


class StaffComplaint(BaseModel):
    """Operational projection for signed-in staff: includes precise location and description,
    never the claim code or its hash, upload-token data or storage keys."""
    id: str
    category: str
    area: str
    description: str
    status: str
    verification: str
    department: str
    created: str
    capturedAt: str | None
    uploadedAt: str
    updated: str
    captureSource: str | None
    mediaType: str
    shareEvidence: bool
    gps: StaffGps
    timeline: list[TimelineEntry]
    verificationLog: list[VerificationEntry]
    evidence: StaffEvidenceSummary | None
    mergedInto: str | None = None


class EvidenceAccessEntry(BaseModel):
    username: str
    displayName: str
    action: str
    accessedAt: str


class EvidenceReceipt(BaseModel):
    """Result of an evidence upload. No storage key, path or URL is ever included."""

    evidenceId: str
    status: Literal["uploaded", "already_uploaded"]
    mediaType: Literal["photo", "video"]
    contentType: str
    byteSize: int
    sha256: str
    capturedAt: str | None
    uploadedAt: str


# ---------- Notifications ----------

class PushConfig(BaseModel):
    pushEnabled: bool
    vapidPublicKey: str | None


class InboxCreated(BaseModel):
    """A citizen notification inbox. The token is shown once; only its hash is stored."""
    inboxToken: str


class PushKeys(_Strict):
    p256dh: str = Field(min_length=80, max_length=100)
    auth: str = Field(min_length=16, max_length=30)


class PushSubscriptionIn(_Strict):
    """The browser's PushSubscription.toJSON(), plus an optional device label."""
    endpoint: str = Field(min_length=12, max_length=2048)
    keys: PushKeys
    expirationTime: int | None = Field(default=None, ge=0)
    label: str = Field(default="", max_length=80)


class PushDevice(BaseModel):
    """A registered device. The endpoint and keys are never returned."""
    subscriptionId: str
    label: str
    createdAt: str
    updatedAt: str
    lastSuccessAt: str | None
    lastFailureAt: str | None
    failureCount: int


class FollowIn(_Strict):
    complaintId: str = Field(max_length=32)
    claim: str = Field(min_length=1, max_length=64)


class FollowOut(BaseModel):
    complaintId: str
    followedAt: str


class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    url: str
    complaintId: str | None
    createdAt: str
    readAt: str | None
    read: bool
    pushStatus: str


class NotificationList(BaseModel):
    items: list[NotificationOut]
    unreadCount: int
    nextBefore: str | None


class UnreadCount(BaseModel):
    unreadCount: int


class ReadAllResult(BaseModel):
    updated: int
    unreadCount: int


# ---------- Complaint management (schema 7) ----------
# Inputs are strict (unknown fields refused, lengths capped). Outputs are explicit allowlists: the
# public projection (PublicComplaint above) is unchanged and never includes any of these fields.

Status = Literal["Submitted", "Under Review", "Assigned", "In Progress", "Marked Resolved"]
WorkOrderStatus = Literal["Open", "In Progress", "Completed", "Cancelled"]
DepartmentCode = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{1,31}$")]
Username = Annotated[str, Field(pattern=r"^[a-z0-9._-]{3,64}$")]


class TransitionIn(_Strict):
    to: Status
    expectedStatus: Status
    explanation: Annotated[str, Field(max_length=500)] = ""


class AssignmentIn(_Strict):
    department: DepartmentCode
    assignee: Username | None = None
    reason: Annotated[str, Field(max_length=500)] = ""
    expectedStatus: Status


class NoteIn(_Strict):
    body: Annotated[str, Field(min_length=1, max_length=2000)]


class PublicUpdateIn(_Strict):
    text: Annotated[str, Field(min_length=1, max_length=500)]
    kind: Literal["progress", "department_response"] = "progress"


class ReinspectionIn(_Strict):
    note: Annotated[str, Field(min_length=1, max_length=500)]
    reopen: bool = False
    expectedStatus: Status


class WorkOrderIn(_Strict):
    team: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=1000)] = ""
    dueAt: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None  # a local (IST) date
    assignee: Username | None = None

    @field_validator("dueAt")
    @classmethod
    def _real_date(cls, v: str | None) -> str | None:
        if v is not None:
            datetime.strptime(v, "%Y-%m-%d")
        return v


class WorkOrderChangeIn(_Strict):
    to: Literal["In Progress", "Completed", "Cancelled"]
    expectedStatus: WorkOrderStatus
    note: Annotated[str, Field(max_length=1000)] = ""


class CitizenResponseIn(_Strict):
    claim: Annotated[str, Field(min_length=1, max_length=100)]
    response: Literal["Appears Resolved", "Still Present", "Cannot Confirm"]


class CitizenResponseOut(BaseModel):
    verification: str
    changed: bool


class DepartmentOut(BaseModel):
    code: str
    name: str
    active: bool


class StaffRef(BaseModel):
    username: str
    displayName: str


class DepartmentRef(BaseModel):
    code: str
    name: str


class AssignmentOut(BaseModel):
    department: DepartmentRef | None = None
    assignee: StaffRef | None = None


class AssignmentHistoryEntry(BaseModel):
    id: str
    department: DepartmentRef
    assignee: str | None
    reason: str
    by: str
    at: str


class WorkOrderEvidence(BaseModel):
    phase: Literal["before", "after"]
    evidenceId: str
    role: str
    contentType: str
    byteSize: int
    sha256: str
    uploadedAt: str


class WorkOrderEvent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None
    note: str
    by: str
    at: str


class WorkOrderOut(BaseModel):
    id: str
    complaintId: str
    department: str | None
    team: str
    assignee: str | None
    description: str
    dueAt: str | None
    status: str
    startedAt: str | None
    completedAt: str | None
    completionNote: str
    createdBy: str
    createdAt: str
    updatedAt: str
    overdue: bool
    evidence: list[WorkOrderEvidence]
    history: list[WorkOrderEvent]


class WorkOrderList(BaseModel):
    items: list[WorkOrderOut]
    total: int
    limit: int
    offset: int


class StaffNoteOut(BaseModel):
    id: str
    body: str
    createdAt: str
    author: StaffRef


class AllowedAction(BaseModel):
    action: str
    label: str
    to: str | None = None
    needsExplanation: bool = False


class MergeOut(BaseModel):
    id: str
    duplicate: str
    canonical: str
    reason: str
    score: float | None
    label: str | None
    by: str
    at: str
    revertedAt: str | None
    revertedBy: str | None
    revertReason: str | None


# ---------- Wards, zones and supervisor operations ----------

WardNumber = Annotated[int, Field(ge=1, le=9999)]
Priority = Literal["low", "normal", "high", "urgent"]
PublicId = Annotated[str, Field(pattern=r"^NCL-\d{4}-[A-Z0-9]{6}$")]


class ZoneRef(BaseModel):
    number: int | None
    name: str | None


class WardRef(BaseModel):
    number: int
    name: str
    zone: ZoneRef


class WardOut(WardRef):
    active: bool
    testing: bool
    hasBoundary: bool


class WardHistoryEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: int | None = Field(default=None, alias="from")
    to: int | None
    method: Literal["auto", "manual", "unverified"]
    reason: str
    gps: dict
    by: str | None
    at: str


class AllocationSide(BaseModel):
    department: str | None
    assignee: str | None
    priority: str | None
    dueAt: str | None
    status: str


class AllocationEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    batch: str
    kind: Literal["allocated", "reallocated"]
    ward: int | None
    zone: int | None
    from_: AllocationSide = Field(alias="from")
    to: AllocationSide
    reason: str
    note: str
    by: str
    at: str


class WardSetIn(_Strict):
    ward: WardNumber
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class AllocationIn(_Strict):
    complaints: Annotated[list[PublicId], Field(min_length=1, max_length=100)]
    department: DepartmentCode | None = None
    assignee: Username
    priority: Priority = "normal"
    dueAt: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None = None  # a local (IST) date
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    note: Annotated[str, Field(max_length=2000)] = ""

    @field_validator("dueAt")
    @classmethod
    def _real_date(cls, v: str | None) -> str | None:
        if v is not None:
            datetime.strptime(v, "%Y-%m-%d")
        return v


class AllocationResult(BaseModel):
    id: str
    outcome: Literal["allocated", "reallocated", "unchanged"]
    allocationId: str | None = None
    status: str | None = None


class AllocationOut(BaseModel):
    batch: str
    results: list[AllocationResult]
    changed: int
    unchanged: int


class ScopeWard(BaseModel):
    number: int
    name: str
    hasBoundary: bool


class ScopeZone(BaseModel):
    number: int
    name: str
    wards: list[ScopeWard]


class SupervisorScope(BaseModel):
    role: str
    cityWide: bool
    zones: list[ScopeZone]
    wardCount: int


class SupervisorComplaint(BaseModel):
    id: str
    title: str
    category: str
    area: str
    status: str
    verification: str
    department: str
    received: str
    ageDays: int
    priority: str | None
    dueAt: str | None
    overdue: bool
    ward: WardRef | None
    wardAssignmentStatus: str
    assignee: StaffRef | None
    gps: dict


class SupervisorComplaintList(BaseModel):
    items: list[SupervisorComplaint]
    total: int
    limit: int
    offset: int


class Counts(BaseModel):
    total: int
    open: int
    resolved: int
    pendingVerification: int
    overdue: int


class CategoryCounts(Counts):
    category: str


class ZoneCounts(Counts):
    number: int
    name: str
    wards: int


class WardCounts(Counts):
    number: int
    name: str
    active: bool
    testing: bool
    zone: ZoneRef


class StatusCount(BaseModel):
    status: str
    count: int


class SupervisorDashboard(BaseModel):
    level: Literal["city", "zone", "ward"]
    cityWide: bool
    zone: int | None
    ward: int | None
    today: str
    totals: Counts
    unverifiedWard: Counts | None
    byStatus: list[StatusCount]
    byCategory: list[CategoryCounts]
    byZone: list[ZoneCounts]
    byWard: list[WardCounts]


class AssignableStaff(BaseModel):
    username: str
    displayName: str
    role: str
    departments: list[str]


class StaffCase(StaffComplaint):
    """StaffComplaint plus management data. Internal notes and staff names appear only here."""
    version: int
    merges: list[MergeOut] = []
    assignment: AssignmentOut
    assignmentHistory: list[AssignmentHistoryEntry]
    workOrders: list[WorkOrderOut]
    notes: list[StaffNoteOut]
    allowedActions: list[AllowedAction]
    ward: WardOut | None = None
    wardAssignmentStatus: Literal["auto", "manual", "unverified"] = "unverified"
    priority: Priority | None = None
    dueAt: str | None = None
    wardHistory: list[WardHistoryEntry] = []
    allocations: list[AllocationEntry] = []


class AuditEntry(BaseModel):
    id: str
    actorType: str
    actor: str | None
    actorName: str | None
    action: str
    details: dict
    at: str


class RepairEvidenceReceipt(BaseModel):
    evidenceId: str
    mediaType: str
    contentType: str
    byteSize: int
    sha256: str
    capturedAt: str | None
    uploadedAt: str


# ---------- Resident accounts (resident_auth.py, residents_api.py) ----------

class ResidentRegister(_Strict):
    email: Annotated[str, Field(min_length=3, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]
    displayName: Annotated[str, Field(min_length=1, max_length=80)]


class ResidentSignIn(_Strict):
    email: Annotated[str, Field(min_length=1, max_length=254)]
    password: Annotated[str, Field(min_length=1, max_length=256)]


class ResidentIdentity(BaseModel):
    """The signed-in resident's own profile. Never returned to anyone else."""
    residentId: str
    email: str
    displayName: str
    demo: bool = False
    createdAt: str | None = None


class ResidentSessionOut(BaseModel):
    token: str
    expiresAt: str
    resident: ResidentIdentity


class ResidentProfileUpdate(_Strict):
    displayName: Annotated[str, Field(min_length=1, max_length=80)]


class OwnerGps(BaseModel):
    lat: float
    lng: float
    accuracy: float | None = None
    sourceType: str | None = None
    address: str | None = None


class OwnerEvidence(BaseModel):
    """Evidence metadata for the complaint's owner. The file itself is fetched with the resident session from
    GET /residents/me/complaints/{id}/evidence/{evidenceId}; never a storage key, path or public URL."""
    evidenceId: str
    role: Literal["original", "repair"]
    mediaType: str
    contentType: str
    byteSize: int
    capturedAt: str | None
    uploadedAt: str


class ResidentComplaint(BaseModel):
    """A complaint as its owning resident sees it: the public fields plus the resident's own description,
    location and evidence list. Never the claim code or hash, upload tokens, staff notes, assignees or storage keys."""
    id: str
    title: str
    category: str
    area: str
    description: str
    mediaType: str
    shareEvidence: bool
    hasEvidence: bool
    gps: OwnerGps
    created: str
    updated: str
    status: str
    verification: str
    department: str
    ownedVia: Literal["submitted", "claimed"]
    ownedAt: str | None
    timeline: list[TimelineEntry]
    verificationLog: list[VerificationEntry]
    evidence: list[OwnerEvidence] = []


class ResidentComplaintList(BaseModel):
    items: list[ResidentComplaint]
    total: int
    limit: int
    offset: int


class ClaimRequest(_Strict):
    id: Annotated[str, Field(min_length=1, max_length=40)]
    claim: Annotated[str, Field(min_length=1, max_length=100)]


class ClaimResult(BaseModel):
    outcome: Literal["claimed", "already_yours"]
    complaint: ResidentComplaint


class ResidentPreferences(_Strict):
    """Preferences the resident workspace already offers (Preferences screen). Stored per account."""
    updates: bool = True
    verification: bool = True
    hotspots: bool = False


# ---------- Duplicate detection (schema 8) ----------

class DuplicateFactor(BaseModel):
    name: str
    value: float | None
    weight: float
    contribution: float
    detail: str


class DuplicateCandidate(BaseModel):
    id: str
    category: str
    area: str
    created: str
    status: str
    verification: str
    score: float
    label: Literal["likely_duplicate", "possible_duplicate", "recurrence", "nearby_separate"]
    distanceM: float
    factors: list[DuplicateFactor]
    reasons: list[str]
    caution: str | None


class DuplicateCandidates(BaseModel):
    complaintId: str
    radiusM: float
    days: int
    candidates: list[DuplicateCandidate]
    note: str | None


class MergeIn(_Strict):
    canonical: Annotated[str, Field(pattern=r"^NCL-\d{4}-[A-Z0-9]{6}$")]
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class ReasonIn(_Strict):
    reason: Annotated[str, Field(min_length=1, max_length=500)]


# ---------- Public community list ----------

class PublicMapPoint(BaseModel):
    """Approximate public position (geo.public_map_point): rounded to about 110 m, never the exact GPS fix."""
    lat: float
    lng: float
    precision: Literal["approx-110m"]


class PublicComplaintSummary(BaseModel):
    """One row of the community list: the public-tracking allowlist without the timeline, plus an
    approximate map point for the community and neighbourhood maps."""
    id: str
    title: str
    category: str
    area: str
    status: str
    verification: str
    department: str
    created: str
    updated: str
    mapPoint: PublicMapPoint | None = None


class Facet(BaseModel):
    name: str
    count: int


class CommunityFacets(BaseModel):
    categories: list[Facet]
    areas: list[Facet]


class PublicComplaintList(BaseModel):
    items: list[PublicComplaintSummary]
    total: int
    limit: int
    offset: int
    facets: CommunityFacets
