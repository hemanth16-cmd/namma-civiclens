"""Complaint notifications: who is told what, persistent history, and Web Push delivery.

Events (all stored in history first, then pushed to every registered device of each recipient):
    citizen (followers of the complaint)   follow_confirmed, evidence_received, evidence_reviewed
    staff (every active staff account)     new_complaint, new_evidence
Each event has a dedupe key, so a recipient is notified about it at most once (for example only
the first time staff open a complaint's evidence). Routes call these methods from FastAPI
background tasks after the response: a notification failure never fails or delays the request
that caused it, and is logged without personal data.

Delivery outcome per device: "sent" resets its failure count; 404/410 from the push service means
the subscription expired or was revoked, so the device is deleted; other failures are counted and
the device is deleted after MAX_FAILURES in a row. Notification bodies contain the public complaint
ID and category only: never descriptions, locations, claim codes or names.
"""

import json
import logging

from .push import PushSender
from .store import ComplaintStore

logger = logging.getLogger("civiclens.notifications")

MAX_FAILURES = 5
MAX_DEVICES = 10   # push subscriptions per recipient
MAX_FOLLOWS = 50   # complaints one citizen inbox can follow


class Notifier:
    def __init__(self, store: ComplaintStore, sender: PushSender | None):
        self.store = store
        self.sender = sender

    # ---------- Events ----------

    def complaint_created(self, public_id: str, category: str) -> None:
        self._emit(public_id, "staff", {
            "kind": "new_complaint", "title": "New complaint", "body": f"{public_id} · {category}",
            "url": f"#/staff/{public_id}", "dedupe": "new_complaint"})

    def evidence_uploaded(self, public_id: str, media_type: str) -> None:
        what = "video" if media_type == "video" else "photo"
        self._emit(public_id, "followers", {
            "kind": "evidence_received", "title": "Evidence received", "body": f"Your {what} for {public_id} was stored privately.",
            "url": f"#/track/{public_id}", "dedupe": "evidence_received"})
        self._emit(public_id, "staff", {
            "kind": "new_evidence", "title": "New evidence", "body": f"A {what} was uploaded for {public_id}.",
            "url": f"#/staff/{public_id}", "dedupe": "new_evidence"})

    def evidence_viewed(self, public_id: str) -> None:
        self._emit(public_id, "followers", {
            "kind": "evidence_reviewed", "title": "Report under review", "body": f"CivicLens staff opened the evidence for {public_id}.",
            "url": f"#/track/{public_id}", "dedupe": "evidence_reviewed"})

    def follow_confirmed(self, recipient_pk: int, public_id: str) -> None:
        self._emit(public_id, "recipient", {
            "kind": "follow_confirmed", "title": "Updates turned on", "body": f"You will be notified about {public_id}.",
            "url": f"#/track/{public_id}", "dedupe": "follow_confirmed", "recipientPk": recipient_pk})

    def test(self, recipient: dict, dedupe_key: str) -> None:
        url = "#/staff/notifications" if recipient["kind"] == "staff" else "#/notifications"
        self._deliver_safely(self._store_safely("recipient", {
            "kind": "test", "title": "Test notification", "body": "Notifications from Namma CivicLens work on this device.",
            "url": url, "dedupeKey": dedupe_key, "recipientPk": recipient["pk"], "audience": recipient["kind"]}))

    # ---------- Internals ----------

    def _emit(self, public_id: str, audience: str, spec: dict) -> None:
        try:
            ref = self.store.complaint_ref(public_id)
        except Exception:  # noqa: BLE001 - background work must never raise
            logger.exception("Notification event could not resolve its complaint")
            return
        if ref is None:
            return
        event = {"kind": spec["kind"], "title": spec["title"], "body": spec["body"], "url": spec["url"],
                 "complaintPk": ref["pk"], "dedupeKey": f"{spec['dedupe']}:{public_id}",
                 "recipientPk": spec.get("recipientPk"), "audience": "staff" if audience == "staff" else "citizen"}
        self._deliver_safely(self._store_safely(audience, event))

    def _store_safely(self, audience: str, event: dict) -> list[dict]:
        try:
            return self.store.add_notifications(audience, event)
        except Exception:  # noqa: BLE001
            logger.exception("Notifications could not be stored (%s)", event["kind"])
            return []

    def _deliver_safely(self, created: list[dict]) -> None:
        for notification in created:
            try:
                self.deliver(notification)
            except Exception:  # noqa: BLE001
                logger.exception("Notification delivery failed unexpectedly")

    def deliver(self, notification: dict) -> str:
        """Push one stored notification to its recipient's devices; records and returns push_status."""
        if self.sender is None:
            status = "disabled"
        elif not notification["devices"]:
            status = "no_devices"
        else:
            payload = json.dumps({
                "id": notification["id"], "kind": notification["kind"], "title": notification["title"],
                "body": notification["body"], "url": notification["url"], "audience": notification["audience"],
                "createdAt": notification["createdAt"]}, separators=(",", ":"))
            sent = 0
            for device in notification["devices"]:
                result = self.sender.send(device, payload)
                self.store.record_push_result(device["pk"], result.outcome, MAX_FAILURES)
                sent += result.outcome == "sent"
            status = "sent" if sent == len(notification["devices"]) else ("partial" if sent else "failed")
        self.store.set_push_status(notification["id"], status)
        return status
