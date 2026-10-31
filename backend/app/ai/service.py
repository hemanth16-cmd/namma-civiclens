"""AiService: schedules and runs AI analysis in the background, outside any request.

Flow for a citizen photo: the upload route commits the file and its evidence row, responds, and only
then (FastAPI BackgroundTasks) calls evidence_added(). That creates one ai_inferences row per routed
detector (state 'pending') and hands the evidence to a small thread pool. A worker claims each row,
reads the file from private storage, strips metadata, sends the pixels to the provider and stores the
normalised result. A provider failure marks the row 'failed' with a code; the complaint and its evidence
are never touched.

After-repair photos follow the same path, and also create a repair_assessments row that compares them
with the original once both sides have results.

Limits (documented in docs/ARCHITECTURE.md): the pool is in-process (CIVICLENS_AI_WORKERS threads, at
most CIVICLENS_AI_MAX_QUEUE queued jobs). Jobs are durable rows, so work that is queued when the process
stops, or that did not fit in the queue, stays 'pending' and is picked up at the next startup or by
POST /staff/complaints/{id}/ai/retry. Claims are conditional updates, so several instances never run the
same job twice.
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from ..lifecycle import RuleViolation
from .ai_store import AiStore
from .image_alignment import align
from .images import ImageProblem, prepare, read_evidence
from .model_registry import AiSettings
from .repair_verifier import assess
from .result_normalizer import normalise
from .roboflow_service import ProviderError, RoboflowClient

logger = logging.getLogger("civiclens.ai")

DISCLOSURE = ("Photos may be analysed by an external AI service (Roboflow) to help staff identify the problem. "
              "Only the image pixels are sent, without location or other metadata. Staff make every decision.")


class InlineExecutor:
    """Runs jobs immediately in the caller's thread (tests and scripts)."""

    def submit(self, fn, *args):
        fn(*args)

    def shutdown(self, wait=True, cancel_futures=False):
        pass


class AiService:
    def __init__(self, store, storages: dict, settings: AiSettings, provider=None, executor=None):
        self.settings = settings
        self.db = AiStore(store)
        self.storages = storages
        self.provider = provider if provider is not None else (RoboflowClient(settings) if settings.enabled else None)
        self._executor = executor   # None: a thread pool, created on first use and again after shutdown()
        self._own = None
        self._lock = threading.Lock()
        self._inflight = 0

    @property
    def enabled(self) -> bool:
        return self.settings.enabled and self.provider is not None

    def status(self) -> dict:
        return {"enabled": self.enabled, "provider": self.settings.provider if self.settings.provider != "off" else None,
                "reason": None if self.enabled else (self.settings.disabled_reason or "disabled"),
                "disclosure": DISCLOSURE if self.enabled else None,
                "thresholds": {"present": self.settings.min_confidence, "low": self.settings.low_confidence}}

    def routing(self, category: str) -> dict:
        route = self.settings.route(category)
        return {"detectors": [s.name for s in self.settings.detectors_for(category)], "relevantCategories": list(route.relevant)}

    # --- Queue ---

    def _submit(self, fn, *args) -> bool:
        with self._lock:
            if self._inflight >= self.settings.max_queue:
                logger.warning("AI queue is full; the job stays pending until a retry or the next startup")
                return False
            self._inflight += 1

        def run():
            try:
                fn(*args)
            except Exception:
                logger.exception("AI job failed unexpectedly")
            finally:
                with self._lock:
                    self._inflight -= 1
        try:
            if self._executor is None:
                self._executor = self._own = ThreadPoolExecutor(self.settings.workers, thread_name_prefix="civiclens-ai")
            self._executor.submit(run)
        except RuntimeError:  # executor shut down
            with self._lock:
                self._inflight -= 1
            return False
        return True

    def start(self) -> None:
        """At startup: resume jobs left pending or lost while running."""
        if not self.enabled:
            return
        try:
            evidence, assessments = self.db.recoverable(self.settings.max_queue)
        except Exception:
            logger.exception("Could not recover pending AI jobs")
            return
        for pk in evidence:
            self._submit(self._run_evidence, pk)
        for pk in assessments:
            self._submit(self._run_assessment, pk)
        if evidence or assessments:
            logger.info("Resumed %d AI evidence job(s) and %d repair assessment(s)", len(evidence), len(assessments))

    def shutdown(self) -> None:
        """Stop the thread pool (queued jobs stay 'pending' in the database and resume at the next start)."""
        if self._own is not None:
            self._own.shutdown(wait=False, cancel_futures=True)
            self._executor = self._own = None
        if self.provider is not None and hasattr(self.provider, "close"):
            self.provider.close()

    # --- Entry points from upload routes (run after the response; never raise) ---

    def evidence_added(self, evidence_id: str) -> None:
        if not self.enabled:
            return
        try:
            pk = self.db.evidence_pk(evidence_id)
            if pk is not None:
                self.schedule_evidence(pk)
        except Exception:
            logger.exception("Could not schedule AI analysis for new evidence")

    def repair_evidence_added(self, evidence_id: str) -> None:
        """A repair photo was attached to a work order: analyse it and compare it with the original."""
        if not self.enabled:
            return
        try:
            pk = self.db.evidence_pk(evidence_id)
            ctx = self.db.evidence_context(pk) if pk is not None else None
            if ctx is None or ctx["role"] != "repair":
                return
            original = self.db.original_evidence(ctx["complaintPk"])
            if original is None or original["mediaType"] != "photo":
                self.schedule_evidence(pk)
                return
            repair = self.db.repair_evidence(ctx["complaintPk"], evidence_id)
            assessment_pk, _ = self.db.create_assessment(ctx["complaintPk"], original["pk"], repair, None)
            self._queue_assessment(assessment_pk, original["pk"], repair["pk"])
        except Exception:
            logger.exception("Could not schedule a repair assessment for new repair evidence")

    # --- Staff actions ---

    def require_enabled(self) -> None:
        if not self.enabled:
            raise RuleViolation(503, "ai_unavailable", "AI analysis is not configured on this server.")

    def retry(self, complaint: dict, staff_pk: int, client_ip: str | None = None) -> int:
        """Re-run failed analyses of this complaint's photos (and create any that are missing). Returns how many were reset."""
        self.require_enabled()
        evidence = [e for e in (self.db.original_evidence(complaint["pk"]),) if e]
        repair = self.db.repair_evidence(complaint["pk"])
        pks = [e["pk"] for e in evidence] + ([repair["pk"]] if repair else [])
        if not pks:
            raise RuleViolation(409, "no_evidence", "This complaint has no evidence to analyse.")
        reset = self.db.reset_for_retry(complaint["pk"], pks)
        assessments = self.db.reset_failed_assessments(complaint["pk"])
        self.db.audit(complaint["pk"], "ai_retry_requested", {"reset": reset, "assessmentsReset": len(assessments)}, staff_pk, client_ip)
        for pk in pks:
            self.schedule_evidence(pk)
        for pk in assessments:
            self._submit(self._run_assessment, pk)
        return reset + len(assessments)

    def request_repair_assessment(self, complaint: dict, repair_evidence_id: str | None, staff_pk: int,
                                  client_ip: str | None = None) -> str:
        self.require_enabled()
        original = self.db.original_evidence(complaint["pk"])
        if original is None:
            raise RuleViolation(409, "no_original_evidence", "The complaint has no original evidence to compare with.")
        if original["mediaType"] != "photo":
            raise RuleViolation(422, "unsupported_media", "The original evidence is a video; repair checks compare photos only.")
        repair = self.db.repair_evidence(complaint["pk"], repair_evidence_id)
        if repair is None:
            raise RuleViolation(409 if repair_evidence_id is None else 404, "no_repair_evidence",
                                "No after-repair photo has been uploaded for this complaint." if repair_evidence_id is None
                                else "No after-repair photo with that ID belongs to this complaint.")
        assessment_pk, _ = self.db.create_assessment(complaint["pk"], original["pk"], repair, staff_pk, client_ip)
        self._queue_assessment(assessment_pk, original["pk"], repair["pk"])
        return assessment_pk

    def _queue_assessment(self, assessment_pk: int, original_pk: int, repair_pk: int) -> None:
        self.schedule_evidence(original_pk)
        self.schedule_evidence(repair_pk)
        self._submit(self._run_assessment, assessment_pk)

    # --- Inference ---

    def schedule_evidence(self, evidence_pk: int) -> None:
        ctx = self.db.evidence_context(evidence_pk)
        if ctx is None:
            return
        specs = self.settings.detectors_for(ctx["category"])
        if not specs:
            return  # no detector for this category (e.g. water leakage): left to people
        if ctx["mediaType"] != "photo" or ctx["contentType"] != "image/jpeg":
            self.db.ensure_inferences(ctx["complaintPk"], evidence_pk, specs, self.settings.provider, skipped_code="unsupported_media")
            return
        self.db.ensure_inferences(ctx["complaintPk"], evidence_pk, specs, self.settings.provider)
        if self.db.pending_inferences(evidence_pk):
            self._submit(self._run_evidence, evidence_pk)

    def _prepare(self, ctx: dict):
        data = read_evidence(self.storages, ctx, self.settings.max_image_bytes)
        return prepare(data, self.settings.max_side_px)

    def _run_evidence(self, evidence_pk: int) -> None:
        ctx = self.db.evidence_context(evidence_pk)
        if ctx is None:
            return
        prepared, problem = None, None
        for pk in self.db.pending_inferences(evidence_pk):
            spec = self.settings.detectors.get(self.db.detector_of(pk))
            if spec is None:
                self.db.fail_inference(pk, "detector_disabled", state="skipped")
                continue
            if self.db.claim_inference(pk, spec) is None:
                continue  # another worker has it
            try:
                if prepared is None and problem is None:
                    try:
                        prepared = self._prepare(ctx)
                    except ImageProblem as exc:
                        problem = exc.code
                if problem:
                    self.db.fail_inference(pk, problem, state="skipped" if problem == "unsupported_media" else "failed")
                    continue
                # Recorded before the image leaves the server.
                self.db.audit(ctx["complaintPk"], "ai_evidence_sent",
                              {"evidenceId": ctx["evidenceId"], "provider": self.settings.provider, "detector": spec.name,
                               "modelId": spec.identifier})
                raw = self.provider.detect(spec, prepared.upload_jpeg)
                result = normalise(raw, self.settings, prepared.upload_size, (prepared.width, prepared.height))
                self.db.complete_inference(pk, result)
            except ProviderError as exc:
                self.db.fail_inference(pk, exc.code)
            except Exception:
                logger.exception("AI inference %s failed", spec.name)
                self.db.fail_inference(pk, "internal_error")
        for assessment_pk in self.db.assessments_waiting_on(evidence_pk):
            self._submit(self._run_assessment, assessment_pk)

    # --- Repair assessment ---

    def _run_assessment(self, assessment_pk: int) -> None:
        inputs = self.db.assessment_inputs(assessment_pk)
        if inputs is None or inputs["state"] != "pending" or not self.db.claim_assessment(assessment_pk):
            return
        try:
            original = self.db.evidence_context(inputs["originalPk"])
            repair = self.db.evidence_context(inputs["repairPk"])
            names = [s.name for s in self.settings.detectors_for(original["category"])]
            o_res = {r["detector"]: r for r in self.db.results_for_evidence(original["pk"]) if r["detector"] in names}
            r_res = {r["detector"]: r for r in self.db.results_for_evidence(repair["pk"]) if r["detector"] in names}
            waiting = [ev for ev, res in ((original["pk"], o_res), (repair["pk"], r_res))
                       if any(n not in res or res[n]["state"] in ("pending", "running") for n in names)]
            if waiting:
                self.db.release_assessment(assessment_pk)  # re-queued when those inferences finish
                for ev in waiting:
                    self.schedule_evidence(ev)
                return
            if any(res[n]["state"] == "failed" for res in (o_res, r_res) for n in names):
                # Not a judgement about the repair: the analysis itself failed. Retry re-runs both.
                self.db.fail_assessment(assessment_pk, "inference_failed")
                return
            try:
                before, after = self._prepare(original), self._prepare(repair)
            except ImageProblem as exc:
                self.db.fail_assessment(assessment_pk, exc.code)
                return
            route = self.settings.route(original["category"])
            result = assess(
                relevant_categories=route.relevant,
                original=[o_res[n] for n in names], repair=[r_res[n] for n in names],
                alignment=align(before.image, after.image),
                complaint_gps=original["complaintGps"], original_gps=before.gps, repair_gps=after.gps,
                quality_original=before.quality, quality_repair=after.quality,
                min_confidence=self.settings.min_confidence, low_confidence=self.settings.low_confidence)
            links = [(o_res[n]["pk"], "original") for n in names] + [(r_res[n]["pk"], "repair") for n in names]
            self.db.complete_assessment(assessment_pk, result, links)
        except Exception:
            logger.exception("Repair assessment failed")
            self.db.fail_assessment(assessment_pk, "internal_error")
