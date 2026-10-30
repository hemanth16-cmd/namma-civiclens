"""Notification API: push configuration, citizen inboxes, follows, device subscriptions, history.

Authorisation: every route except /config and inbox creation needs a bearer token in the
Authorization header: either a staff session token (the staff member's notifications) or a citizen
inbox token from POST /notifications/inboxes (notifications for complaints that inbox follows).
Both are stored only as SHA-256 hashes. A recipient can read, mark and delete only its own
notifications and devices; another recipient's IDs answer 404, exactly like unknown IDs.

Citizens have no accounts: an inbox is a device-held credential. Following a complaint requires its
private claim code (checked with scrypt, rate-limited like upload-token renewal). Staff receive
staff events automatically and cannot follow complaints.
"""

import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response

from .auth import bearer_token
from .claims import hash_upload_token, verify_claim_code
from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .models import (FollowIn, FollowOut, InboxCreated, NotificationList, NotificationOut, PushConfig, PushDevice,
                     PushSubscriptionIn, ReadAllResult, UnreadCount)
from .notifications import MAX_DEVICES, MAX_FOLLOWS
from .push import b64url_decode, endpoint_host_allowed
from .ratelimit import enforce
from .store import NOTIFICATION_PAGE_MAX, iso

router = APIRouter(prefix="/notifications", tags=["notifications"])
NOTIFICATION_ID = re.compile(r"^nt_[A-Za-z0-9_-]{16,40}$")
SUBSCRIPTION_ID = re.compile(r"^ps_[A-Za-z0-9_-]{16,40}$")
LABEL = re.compile(r"^[\w .,()/+-]*$")
NOTIFICATION_NOT_FOUND = "No notification with that ID was found."


def current_recipient(request: Request) -> dict:
    """FastAPI dependency: the staff member or citizen inbox that owns the bearer token, or 401."""
    store = request.app.state.store
    token = bearer_token(request)
    if token:
        token_hash = hash_upload_token(token)
        session = store.get_staff_session(token_hash)
        if session is not None:
            return store.staff_recipient(session["pk"])
        inbox = store.citizen_recipient(token_hash)
        if inbox is not None:
            return inbox
    raise ApiProblem(401, "unauthorized", "Notifications need a staff session or this device's notification inbox token.",
                     headers={"WWW-Authenticate": "Bearer"})


def _citizen(recipient: dict) -> dict:
    if recipient["kind"] != "citizen":
        raise ApiProblem(403, "forbidden", "Staff accounts receive staff notifications automatically and cannot follow complaints.")
    return recipient


@router.get("/config", response_model=PushConfig)
def push_config(request: Request) -> dict:
    """Public: whether Web Push is configured, and the VAPID public key browsers subscribe with."""
    settings = request.app.state.push_settings
    return {"pushEnabled": settings is not None, "vapidPublicKey": settings.public_key if settings else None}


@router.post("/inboxes", status_code=201, response_model=InboxCreated)
def create_inbox(request: Request, response: Response) -> dict:
    enforce(request, "inbox_per_ip", "ip")
    token = secrets.token_urlsafe(32)
    request.app.state.store.create_citizen_recipient(hash_upload_token(token))
    response.headers["Cache-Control"] = "no-store"
    return {"inboxToken": token}


# ---------- History ----------

@router.get("", response_model=NotificationList)
def list_notifications(request: Request, response: Response, recipient: dict = Depends(current_recipient),
                       limit: int = Query(30, ge=1, le=NOTIFICATION_PAGE_MAX), before: str | None = Query(None, max_length=48),
                       unread: bool = False) -> dict:
    if before is not None and not NOTIFICATION_ID.fullmatch(before):
        raise ApiProblem(422, "validation_error", "before must be a notification ID.")
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.store.list_notifications(recipient["pk"], limit, before, unread)


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(request: Request, response: Response, recipient: dict = Depends(current_recipient)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"unreadCount": request.app.state.store.unread_count(recipient["pk"])}


def _mark(request: Request, recipient: dict, notification_id: str, read: bool) -> dict:
    if not NOTIFICATION_ID.fullmatch(notification_id):
        raise ApiProblem(404, "not_found", NOTIFICATION_NOT_FOUND)
    row = request.app.state.store.mark_notification(recipient["pk"], notification_id, read)
    if row is None:
        raise ApiProblem(404, "not_found", NOTIFICATION_NOT_FOUND)
    return row


@router.post("/read-all", response_model=ReadAllResult)
def read_all(request: Request, recipient: dict = Depends(current_recipient)) -> dict:
    return {"updated": request.app.state.store.mark_all_notifications_read(recipient["pk"]), "unreadCount": 0}


@router.post("/{notification_id}/read", response_model=NotificationOut)
def mark_read(notification_id: str, request: Request, recipient: dict = Depends(current_recipient)) -> dict:
    return _mark(request, recipient, notification_id, True)


@router.post("/{notification_id}/unread", response_model=NotificationOut)
def mark_unread(notification_id: str, request: Request, recipient: dict = Depends(current_recipient)) -> dict:
    return _mark(request, recipient, notification_id, False)


# ---------- Devices (push subscriptions) ----------

def _validated_subscription(request: Request, payload: PushSubscriptionIn) -> dict:
    parts = urlsplit(payload.endpoint)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
        raise ApiProblem(422, "invalid_subscription", "The push endpoint must be an https URL of a browser push service.")
    if not endpoint_host_allowed(parts.hostname, request.app.state.push_endpoint_hosts):
        raise ApiProblem(422, "unsupported_push_service", "This browser's push service is not on the server's allowed list.")
    try:
        p256dh, auth = b64url_decode(payload.keys.p256dh), b64url_decode(payload.keys.auth)
    except ValueError:
        raise ApiProblem(422, "invalid_subscription", "The subscription keys are not valid base64url.") from None
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise ApiProblem(422, "invalid_subscription", "The subscription keys have the wrong size.")
    if not LABEL.fullmatch(payload.label):
        raise ApiProblem(422, "invalid_subscription", "The device label may contain only letters, digits, spaces and . , ( ) / + -")
    expires = None
    if payload.expirationTime is not None:
        try:
            expires = iso(datetime.fromtimestamp(payload.expirationTime / 1000, timezone.utc))
        except (OverflowError, OSError, ValueError):
            raise ApiProblem(422, "invalid_subscription", "expirationTime is out of range.") from None
    return {"endpoint": payload.endpoint, "p256dh": payload.keys.p256dh, "auth": payload.keys.auth,
            "label": payload.label, "expiresAt": expires}


@router.get("/subscriptions", response_model=list[PushDevice])
def list_devices(request: Request, response: Response, recipient: dict = Depends(current_recipient)) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.store.list_push_subscriptions(recipient["pk"])


@router.post("/subscriptions", status_code=201, response_model=PushDevice)
def register_device(payload: PushSubscriptionIn, request: Request, response: Response,
                    recipient: dict = Depends(current_recipient)) -> dict:
    """Register this browser for push. Registering the same endpoint again updates it (200), so
    a retry or a re-subscription never creates a duplicate device."""
    enforce(request, "push_register_per_ip", "ip")
    sub = _validated_subscription(request, payload)
    outcome, device = request.app.state.store.save_push_subscription(recipient["pk"], sub, MAX_DEVICES)
    if outcome == "limit":
        raise ApiProblem(409, "too_many_devices", f"At most {MAX_DEVICES} devices can receive notifications. Remove one first.")
    if outcome == "updated":
        response.status_code = 200
    return device


@router.delete("/subscriptions/{subscription_id}", status_code=204)
def remove_device(subscription_id: str, request: Request, recipient: dict = Depends(current_recipient)) -> Response:
    if not SUBSCRIPTION_ID.fullmatch(subscription_id) or not request.app.state.store.delete_push_subscription(recipient["pk"], subscription_id):
        raise ApiProblem(404, "not_found", "No device with that ID is registered.")
    return Response(status_code=204)


# ---------- Follows (citizens) ----------

@router.get("/follows", response_model=list[FollowOut])
def list_follows(request: Request, response: Response, recipient: dict = Depends(current_recipient)) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    return request.app.state.store.list_follows(_citizen(recipient)["pk"])


@router.post("/follows", status_code=201, response_model=FollowOut)
def follow(payload: FollowIn, request: Request, response: Response, background: BackgroundTasks,
           recipient: dict = Depends(current_recipient)) -> dict:
    """Follow a complaint with its private claim code. Following again answers 200 without a new
    confirmation notification."""
    _citizen(recipient)
    enforce(request, "follow_per_ip", "ip")
    public_id = payload.complaintId.strip().upper()
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    enforce(request, "follow_per_complaint", public_id)
    store = request.app.state.store
    ref = store.complaint_ref(public_id)
    if ref is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    if not verify_claim_code(payload.claim, ref["claimHash"]):
        raise ApiProblem(401, "unauthorized", "The claim code does not match this complaint.")
    outcome = store.follow_complaint(recipient["pk"], ref["pk"], MAX_FOLLOWS)
    if outcome == "limit":
        raise ApiProblem(409, "too_many_follows", f"One device can follow at most {MAX_FOLLOWS} complaints.")
    if outcome == "created":
        background.add_task(request.app.state.notifier.follow_confirmed, recipient["pk"], public_id)
    else:
        response.status_code = 200
    followed = next(f for f in store.list_follows(recipient["pk"]) if f["complaintId"] == public_id)
    return followed


@router.delete("/follows/{public_id}", status_code=204)
def unfollow(public_id: str, request: Request, recipient: dict = Depends(current_recipient)) -> Response:
    _citizen(recipient)
    if not PUBLIC_ID_PATTERN.fullmatch(public_id) or not request.app.state.store.unfollow_complaint(recipient["pk"], public_id):
        raise ApiProblem(404, "not_found", "This device does not follow that complaint.")
    return Response(status_code=204)


# ---------- Test ----------

@router.post("/test", status_code=202)
def send_test(request: Request, background: BackgroundTasks, recipient: dict = Depends(current_recipient)) -> dict:
    """Store a test notification and push it to the caller's devices (for checking a device)."""
    enforce(request, "push_test_per_recipient", f"recipient:{recipient['pk']}")
    background.add_task(request.app.state.notifier.test, recipient, "test:" + secrets.token_urlsafe(12))
    return {"status": "queued", "pushEnabled": request.app.state.push_settings is not None}
