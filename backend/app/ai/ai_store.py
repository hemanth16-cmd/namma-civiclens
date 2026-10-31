"""SQL for AI analysis: ai_inferences, repair_assessments and repair_assessment_inferences (schema 8).

Kept beside store.py rather than inside it so the AI feature does not grow the complaint store, but it
uses the same database object, placeholders, transactions and audit log (ComplaintStore._audit). The
tables are created by store.MIGRATIONS[8] / POSTGRES_MIGRATIONS[8].

Idempotency: an inference is unique per (evidence, detector) and an assessment per (original, repair)
evidence pair, and inserts use ON CONFLICT DO NOTHING. Work is claimed with a conditional UPDATE
(state 'pending' -> 'running'), so two workers, or two server instances, never run the same job.
"""

import json
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone

from ..db import DB_ERRORS
from ..lifecycle import RuleViolation
from ..store import ComplaintStore, StoreError, _evidence_dict, iso, utc_now
from .. import lifecycle as lc

STALE_RUNNING = timedelta(minutes=10)   # a job 'running' longer than this is assumed lost (e.g. a restart)
MAX_ATTEMPTS = 5                        # retries stop here; staff see the error code


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _load(value, default):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value  # PostgreSQL JSONB arrives parsed
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _inference_dict(r) -> dict:
    return {
        "id": r["inference_id"], "evidenceId": r["evidence_id"], "evidenceRole": r["role"], "detector": r["detector"],
        "provider": r["provider"], "mechanism": r["mechanism"], "modelId": r["model_id"], "modelVersion": r["model_version"],
        "classPrompt": r["class_prompt"], "state": r["state"], "originalClass": r["original_class"],
        "detectedCategory": r["detected_category"], "topConfidence": r["top_confidence"], "detectionCount": r["detection_count"],
        "detections": _load(r["predictions"], []), "confidence": _load(r["confidence_meta"], {}),
        "imageWidth": r["image_width"], "imageHeight": r["image_height"], "errorCode": r["error_code"], "attempts": r["attempts"],
        "createdAt": r["created_at"], "startedAt": r["started_at"], "completedAt": r["completed_at"],
    }


_INFERENCE_SELECT = ("SELECT i.*, e.evidence_id, e.role FROM ai_inferences i JOIN evidence e ON e.pk = i.evidence_pk ")
_ASSESSMENT_SELECT = (
    "SELECT a.*, eo.evidence_id AS original_evidence_id, er.evidence_id AS repair_evidence_id, w.work_order_id, "
    "rq.username AS requested_by, rv.username AS reviewer, rv.display_name AS reviewer_name, er.uploaded_by_staff_pk AS repair_uploader_pk "
    "FROM repair_assessments a JOIN evidence eo ON eo.pk = a.original_evidence_pk JOIN evidence er ON er.pk = a.repair_evidence_pk "
    "LEFT JOIN work_orders w ON w.pk = a.work_order_pk LEFT JOIN staff_accounts rq ON rq.pk = a.requested_by_staff_pk "
    "LEFT JOIN staff_accounts rv ON rv.pk = a.reviewer_staff_pk ")


def verified_resolution(outcome: str | None, review: str) -> str:
    """The human-confirmed repair state, separate from the complaint's administrative status."""
    if review == "approved" and outcome == "likely_repaired":
        return "verified_repaired"
    if review == "approved" and outcome == "damage_visible":
        return "verified_not_repaired"
    return "unverified"


def _assessment_dict(r) -> dict:
    return {
        "id": r["assessment_id"], "workOrderId": r["work_order_id"], "originalEvidenceId": r["original_evidence_id"],
        "repairEvidenceId": r["repair_evidence_id"], "state": r["state"], "suggestedOutcome": r["suggested_outcome"],
        "verificationConfidence": r["verification_confidence"], "locationCheck": r["location_check"],
        "gpsDistanceM": r["gps_distance_m"], "details": _load(r["details"], {}), "errorCode": r["error_code"],
        "attempts": r["attempts"], "requestedBy": r["requested_by"],
        "review": {"status": r["review_status"], "reviewer": r["reviewer"], "reviewerName": r["reviewer_name"],
                   "note": r["review_note"], "reviewedAt": r["reviewed_at"]},
        "verifiedResolution": verified_resolution(r["suggested_outcome"], r["review_status"]),
        "createdAt": r["created_at"], "completedAt": r["completed_at"], "updatedAt": r["updated_at"],
    }


class AiStore:
    def __init__(self, store: ComplaintStore):
        self.store = store

    def _read(self, what: str, fn):
        try:
            with closing(self.store._connect()) as conn:
                return fn(conn)
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError(f"{what} failed") from exc

    def _tx(self, what: str, fn, immediate: bool = False):
        try:
            with closing(self.store._connect()) as conn, conn.transaction(immediate=immediate):
                return fn(conn)
        except RuleViolation:
            raise
        except DB_ERRORS as exc:
            raise StoreError(f"{what} failed") from exc

    # --- Complaint and evidence lookups ---

    def complaint_for_staff(self, public_id: str, staff_pk: int) -> dict | None:
        """{"pk", "id", "category", "gps", "status"} if the staff member may see the complaint; 403 otherwise."""
        def run(conn):
            row = conn.execute("SELECT pk, public_id, category, gps_lat, gps_lng, status FROM complaints WHERE public_id = ?",
                               (public_id,)).fetchone()
            if row is None:
                return None
            lc.require_scope(self.store._case(conn, public_id), self.store._actor(conn, staff_pk))
            return {"pk": row["pk"], "id": row["public_id"], "category": row["category"],
                    "gps": (row["gps_lat"], row["gps_lng"]), "status": row["status"]}
        return self._read("Complaint lookup", run)

    def evidence_context(self, evidence_pk: int) -> dict | None:
        """The evidence row (store format, with pk) plus its complaint's category, GPS and public ID."""
        def run(conn):
            row = conn.execute("SELECT e.*, c.public_id, c.category, c.gps_lat, c.gps_lng FROM evidence e "
                               "JOIN complaints c ON c.pk = e.complaint_pk WHERE e.pk = ?", (evidence_pk,)).fetchone()
            if row is None:
                return None
            return _evidence_dict(row) | {"pk": row["pk"], "complaintId": row["public_id"], "category": row["category"],
                                          "complaintGps": (row["gps_lat"], row["gps_lng"])}
        return self._read("Evidence lookup", run)

    def evidence_pk(self, evidence_id: str) -> int | None:
        return self._read("Evidence lookup", lambda conn: (lambda r: r[0] if r else None)(
            conn.execute("SELECT pk FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()))

    def original_evidence(self, complaint_pk: int) -> dict | None:
        def run(conn):
            row = conn.execute("SELECT * FROM evidence WHERE complaint_pk = ? AND role = 'original'", (complaint_pk,)).fetchone()
            return _evidence_dict(row) | {"pk": row["pk"]} if row else None
        return self._read("Evidence lookup", run)

    def repair_evidence(self, complaint_pk: int, evidence_id: str | None = None) -> dict | None:
        """The named repair photo of this complaint, or its latest one. Includes the linked work order's pk."""
        def run(conn):
            sql = ("SELECT e.*, (SELECT l.work_order_pk FROM work_order_evidence l WHERE l.evidence_pk = e.pk ORDER BY l.pk LIMIT 1) "
                   "AS work_order_pk FROM evidence e WHERE e.complaint_pk = ? AND e.role = 'repair'")
            params = (complaint_pk,)
            if evidence_id:
                sql += " AND e.evidence_id = ?"
                params += (evidence_id,)
            row = conn.execute(sql + " ORDER BY e.pk DESC LIMIT 1", params).fetchone()
            return _evidence_dict(row) | {"pk": row["pk"], "workOrderPk": row["work_order_pk"]} if row else None
        return self._read("Evidence lookup", run)

    # --- Inferences ---

    def ensure_inferences(self, complaint_pk: int, evidence_pk: int, specs, provider: str, skipped_code: str | None = None) -> None:
        """Create one row per detector if missing (never duplicates). With skipped_code, rows are created
        in state 'skipped' (for example a video)."""
        def run(conn):
            now = utc_now()
            for spec in specs:
                conn.execute(
                    "INSERT INTO ai_inferences (inference_id, complaint_pk, evidence_pk, detector, provider, mechanism, model_id, "
                    "model_version, class_prompt, state, error_code, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (evidence_pk, detector) DO NOTHING",
                    ("ai_" + secrets.token_urlsafe(16), complaint_pk, evidence_pk, spec.name, provider, spec.mechanism, spec.identifier,
                     spec.version, spec.classes or None, "skipped" if skipped_code else "pending", skipped_code, now, now))
        self._tx("AI job scheduling", run)

    def pending_inferences(self, evidence_pk: int) -> list[int]:
        return self._read("AI job lookup", lambda conn: [r[0] for r in conn.execute(
            "SELECT pk FROM ai_inferences WHERE evidence_pk = ? AND state = 'pending' ORDER BY pk", (evidence_pk,))])

    def claim_inference(self, pk: int, spec) -> dict | None:
        """Mark a pending job as running (recording the detector configuration used now); None if another worker has it."""
        def run(conn):
            now = utc_now()
            n = conn.execute("UPDATE ai_inferences SET state = 'running', attempts = attempts + 1, started_at = ?, updated_at = ?, "
                             "mechanism = ?, model_id = ?, model_version = ?, class_prompt = ?, error_code = NULL "
                             "WHERE pk = ? AND state = 'pending'",
                             (now, now, spec.mechanism, spec.identifier, spec.version, spec.classes or None, pk)).rowcount
            if n != 1:
                return None
            row = conn.execute("SELECT pk, evidence_pk, complaint_pk, detector FROM ai_inferences WHERE pk = ?", (pk,)).fetchone()
            return {"pk": row["pk"], "evidencePk": row["evidence_pk"], "complaintPk": row["complaint_pk"], "detector": row["detector"]}
        return self._tx("AI job claim", run)

    def detector_of(self, pk: int) -> str | None:
        return self._read("AI job lookup", lambda conn: (lambda r: r[0] if r else None)(
            conn.execute("SELECT detector FROM ai_inferences WHERE pk = ?", (pk,)).fetchone()))

    def complete_inference(self, pk: int, result: dict) -> None:
        def run(conn):
            now = utc_now()
            conn.execute(
                "UPDATE ai_inferences SET state = 'completed', original_class = ?, detected_category = ?, top_confidence = ?, "
                "detection_count = ?, predictions = ?, confidence_meta = ?, image_width = ?, image_height = ?, error_code = NULL, "
                "completed_at = ?, updated_at = ? WHERE pk = ?",
                (result["topClass"], result["topCategory"], result["topConfidence"], len(result["detections"]),
                 _dump(result["detections"]), _dump(result["confidenceMeta"]), result["imageWidth"], result["imageHeight"], now, now, pk))
        self._tx("AI result", run)

    def fail_inference(self, pk: int, code: str, state: str = "failed") -> None:
        def run(conn):
            now = utc_now()
            conn.execute("UPDATE ai_inferences SET state = ?, error_code = ?, completed_at = ?, updated_at = ? WHERE pk = ?",
                         (state, code[:64], now, now, pk))
        self._tx("AI result", run)

    def results_for_evidence(self, evidence_pk: int) -> list[dict]:
        return self._read("AI results", lambda conn: [
            _inference_dict(r) | {"pk": r["pk"]} for r in conn.execute(_INFERENCE_SELECT + "WHERE i.evidence_pk = ? ORDER BY i.pk", (evidence_pk,))])

    def results_for_complaint(self, complaint_pk: int) -> list[dict]:
        return self._read("AI results", lambda conn: [
            _inference_dict(r) for r in conn.execute(_INFERENCE_SELECT + "WHERE i.complaint_pk = ? ORDER BY i.pk", (complaint_pk,))])

    def reset_for_retry(self, complaint_pk: int, evidence_pks: list[int]) -> int:
        """Failed (and lost 'running') jobs go back to 'pending', up to MAX_ATTEMPTS. Returns how many."""
        if not evidence_pks:
            return 0
        def run(conn):
            stale = iso(datetime.now(timezone.utc) - STALE_RUNNING)
            marks = ", ".join("?" * len(evidence_pks))
            return conn.execute(
                f"UPDATE ai_inferences SET state = 'pending', error_code = NULL, updated_at = ? WHERE complaint_pk = ? "
                f"AND evidence_pk IN ({marks}) AND attempts < ? AND (state = 'failed' OR (state = 'running' AND updated_at < ?))",
                (utc_now(), complaint_pk, *evidence_pks, MAX_ATTEMPTS, stale)).rowcount
        return self._tx("AI retry", run)

    def recoverable(self, limit: int) -> tuple[list[int], list[int]]:
        """At startup: lost 'running' jobs become 'pending'. Returns (evidence pks with pending inferences,
        pending assessment pks), oldest first."""
        def run(conn):
            stale, now = iso(datetime.now(timezone.utc) - STALE_RUNNING), utc_now()
            conn.execute("UPDATE ai_inferences SET state = 'pending', updated_at = ? WHERE state = 'running' AND updated_at < ?", (now, stale))
            conn.execute("UPDATE repair_assessments SET state = 'pending', updated_at = ? WHERE state = 'running' AND updated_at < ?", (now, stale))
            ev = [r[0] for r in conn.execute("SELECT DISTINCT evidence_pk FROM ai_inferences WHERE state = 'pending' ORDER BY evidence_pk LIMIT ?", (limit,))]
            asm = [r[0] for r in conn.execute("SELECT pk FROM repair_assessments WHERE state = 'pending' ORDER BY pk LIMIT ?", (limit,))]
            return ev, asm
        return self._tx("AI recovery", run)

    def audit(self, complaint_pk: int, action: str, details: dict, staff_pk: int | None = None, client_ip: str | None = None) -> None:
        def run(conn):
            actor = self.store._actor(conn, staff_pk) if staff_pk else None
            self.store._audit(conn, complaint_pk, actor, action, details, client_ip, utc_now())
        self._tx("Audit", run)

    # --- Repair assessments ---

    def create_assessment(self, complaint_pk: int, original_pk: int, repair: dict, staff_pk: int | None, client_ip: str | None = None) -> tuple[int, bool]:
        """(assessment pk, created). The same pair is never assessed twice; a failed one is re-queued."""
        def run(conn):
            now = utc_now()
            actor = self.store._actor(conn, staff_pk) if staff_pk else None
            existing = conn.execute("SELECT pk, state FROM repair_assessments WHERE original_evidence_pk = ? AND repair_evidence_pk = ?",
                                    (original_pk, repair["pk"])).fetchone()
            if existing:
                if existing["state"] == "failed":
                    conn.execute("UPDATE repair_assessments SET state = 'pending', error_code = NULL, updated_at = ? WHERE pk = ?", (now, existing["pk"]))
                    return existing["pk"], True
                return existing["pk"], False
            assessment_id = "ra_" + secrets.token_urlsafe(16)
            pk = conn.insert(
                "INSERT INTO repair_assessments (assessment_id, complaint_pk, work_order_pk, original_evidence_pk, repair_evidence_pk, "
                "requested_by_staff_pk, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (assessment_id, complaint_pk, repair.get("workOrderPk"), original_pk, repair["pk"], actor["pk"] if actor else None, now, now))
            self.store._audit(conn, complaint_pk, actor, "repair_assessment_requested",
                              {"assessmentId": assessment_id, "repairEvidenceId": repair["evidenceId"]}, client_ip, now)
            return pk, True
        return self._tx("Repair assessment", run, immediate=True)

    def reset_failed_assessments(self, complaint_pk: int) -> list[int]:
        def run(conn):
            pks = [r[0] for r in conn.execute("SELECT pk FROM repair_assessments WHERE complaint_pk = ? AND state = 'failed' AND attempts < ?",
                                              (complaint_pk, MAX_ATTEMPTS))]
            for pk in pks:
                conn.execute("UPDATE repair_assessments SET state = 'pending', error_code = NULL, updated_at = ? WHERE pk = ?", (utc_now(), pk))
            return pks
        return self._tx("Repair assessment retry", run)

    def assessment_inputs(self, pk: int) -> dict | None:
        def run(conn):
            r = conn.execute("SELECT pk, complaint_pk, original_evidence_pk, repair_evidence_pk, state FROM repair_assessments WHERE pk = ?", (pk,)).fetchone()
            return {"pk": r["pk"], "complaintPk": r["complaint_pk"], "originalPk": r["original_evidence_pk"],
                    "repairPk": r["repair_evidence_pk"], "state": r["state"]} if r else None
        return self._read("Repair assessment", run)

    def claim_assessment(self, pk: int) -> bool:
        def run(conn):
            now = utc_now()
            return conn.execute("UPDATE repair_assessments SET state = 'running', attempts = attempts + 1, updated_at = ? "
                                "WHERE pk = ? AND state = 'pending'", (now, pk)).rowcount == 1
        return self._tx("Repair assessment claim", run)

    def release_assessment(self, pk: int) -> None:
        """Back to 'pending' (its inputs are not ready yet); re-queued when they are."""
        self._tx("Repair assessment", lambda conn: conn.execute(
            "UPDATE repair_assessments SET state = 'pending', updated_at = ? WHERE pk = ? AND state = 'running'", (utc_now(), pk)))

    def complete_assessment(self, pk: int, result: dict, inference_pks: list[tuple[int, str]]) -> None:
        def run(conn):
            now = utc_now()
            conn.execute(
                "UPDATE repair_assessments SET state = 'completed', suggested_outcome = ?, verification_confidence = ?, location_check = ?, "
                "gps_distance_m = ?, details = ?, error_code = NULL, completed_at = ?, updated_at = ? WHERE pk = ?",
                (result["outcome"], result["verificationConfidence"], result["locationCheck"], result["gpsDistanceM"],
                 _dump(result["details"]), now, now, pk))
            for inference_pk, phase in inference_pks:
                conn.execute("INSERT INTO repair_assessment_inferences (assessment_pk, inference_pk, phase) VALUES (?, ?, ?) "
                             "ON CONFLICT (assessment_pk, inference_pk) DO NOTHING", (pk, inference_pk, phase))
            row = conn.execute("SELECT complaint_pk, assessment_id FROM repair_assessments WHERE pk = ?", (pk,)).fetchone()
            self.store._audit(conn, row["complaint_pk"], None, "repair_assessment_completed",
                              {"assessmentId": row["assessment_id"], "suggestedOutcome": result["outcome"],
                               "verificationConfidence": result["verificationConfidence"]}, None, now)
        self._tx("Repair assessment result", run)

    def fail_assessment(self, pk: int, code: str) -> None:
        self._tx("Repair assessment result", lambda conn: conn.execute(
            "UPDATE repair_assessments SET state = 'failed', error_code = ?, updated_at = ? WHERE pk = ?", (code[:64], utc_now(), pk)))

    def assessments_waiting_on(self, evidence_pk: int) -> list[int]:
        return self._read("Repair assessment", lambda conn: [r[0] for r in conn.execute(
            "SELECT pk FROM repair_assessments WHERE state = 'pending' AND (original_evidence_pk = ? OR repair_evidence_pk = ?)",
            (evidence_pk, evidence_pk))])

    def assessments_for_complaint(self, complaint_pk: int) -> list[dict]:
        return self._read("Repair assessments", lambda conn: [
            _assessment_dict(r) for r in conn.execute(_ASSESSMENT_SELECT + "WHERE a.complaint_pk = ? ORDER BY a.pk DESC", (complaint_pk,))])

    def assessment(self, assessment_id: str) -> dict | None:
        return self._read("Repair assessment", lambda conn: (lambda r: _assessment_dict(r) if r else None)(
            conn.execute(_ASSESSMENT_SELECT + "WHERE a.assessment_id = ?", (assessment_id,)).fetchone()))

    def review_assessment(self, public_id: str, assessment_id: str, staff_pk: int, decision: str, note: str,
                          client_ip: str | None = None) -> None:
        """A supervisor (or admin) approves or rejects the AI suggestion. The complaint itself is not changed."""
        def run(conn):
            now = utc_now()
            actor = self.store._actor(conn, staff_pk)
            case = self.store._case(conn, public_id, lock=True)
            if case is None:
                raise RuleViolation(404, "not_found", "No complaint was found with that ID.")
            lc.require_scope(case, actor)
            lc.require_role(actor, "supervisor", "review an AI repair assessment")
            row = conn.execute("SELECT a.pk, a.state, a.review_status, a.suggested_outcome, e.uploaded_by_staff_pk FROM repair_assessments a "
                               "JOIN evidence e ON e.pk = a.repair_evidence_pk WHERE a.assessment_id = ? AND a.complaint_pk = ?"
                               + self.store.db.for_update, (assessment_id, case["pk"])).fetchone()
            if row is None:
                raise RuleViolation(404, "not_found", "No repair assessment was found with that ID for this complaint.")
            if row["state"] != "completed":
                raise RuleViolation(409, "assessment_not_ready", "The AI assessment has not finished, so there is nothing to review yet.")
            if row["review_status"] != "pending_review":
                raise RuleViolation(409, "already_reviewed", "This assessment has already been reviewed.")
            if row["uploaded_by_staff_pk"] is not None and row["uploaded_by_staff_pk"] == actor["pk"]:
                raise RuleViolation(403, "forbidden", "The person who uploaded the repair photo cannot review its assessment.")
            if decision == "rejected" and not note.strip():
                raise RuleViolation(422, "validation_error", "Explain why the AI suggestion is rejected.")
            conn.execute("UPDATE repair_assessments SET review_status = ?, reviewer_staff_pk = ?, review_note = ?, reviewed_at = ?, updated_at = ? "
                         "WHERE pk = ?", (decision, actor["pk"], note.strip()[:1000], now, now, row["pk"]))
            self.store._audit(conn, case["pk"], actor, "repair_assessment_reviewed",
                              {"assessmentId": assessment_id, "decision": decision, "suggestedOutcome": row["suggested_outcome"],
                               "verifiedResolution": verified_resolution(row["suggested_outcome"], decision)}, client_ip, now)
        self._tx("Repair assessment review", run, immediate=True)
