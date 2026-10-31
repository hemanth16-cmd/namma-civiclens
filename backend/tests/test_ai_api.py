"""AI analysis end to end through HTTP on the configured database (SQLite, or PostgreSQL with
CIVICLENS_TEST_DATABASE=postgres): citizen photo -> private storage -> detection -> stored result ->
staff dashboard; repair photo on a work order -> before/after assessment -> supervisor review.
The provider is a deterministic fake (tests/aiutil.py) and jobs run inline, so nothing leaves the machine."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.ai.model_registry import AiSettings
from app.ai.service import InlineExecutor
from app.main import create_app
from tests import dbutil
from tests.aiutil import COMPLAINT_GPS, TEST_KEY, FakeProvider, NullExecutor, ai_settings, photo
from tests.test_cases_api import API, act, assign, cli, login, org  # noqa: F401  (org is a fixture)
from tests.test_complaints import payload
from tests.test_evidence import MP4


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def make_api(isolated_db_path, evidence_dir):
    clients = []

    def make(settings=None, provider=None, executor=None):
        app = create_app(db_path=isolated_db_path, evidence_dir=evidence_dir,
                         ai_settings=settings if settings is not None else ai_settings(),
                         ai_provider=provider, ai_executor=executor or InlineExecutor())
        client = TestClient(app, client=("10.0.0.7", 5000))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api, org, provider):  # noqa: F811
    return make_api(provider=provider)


@pytest.fixture
def staff(api):
    return {u: login(api, u) for u in ("asha", "bilal", "ravi", "meera")}


def report(api, content=None, media_type="photo", category="Potholes"):
    receipt = api.post(f"{API}/complaints", json=payload(mediaType=media_type, category=category)).json()
    name, ctype = ("e.jpg", "image/jpeg") if media_type == "photo" else ("e.mp4", "video/mp4")
    up = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": (name, content or photo(), ctype)},
                  headers={"Authorization": "Bearer " + receipt["uploadToken"]})
    assert up.status_code == 201, up.text
    return receipt["id"], up.json()["evidenceId"]


def analysis(api, headers, cid, expect=200):
    r = api.get(f"{API}/staff/complaints/{cid}/ai", headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def work_order(api, staff, cid):
    assign(api, staff["ravi"], cid, assignee="asha")
    case = act(api, staff["asha"], f"/complaints/{cid}/work-orders", {"team": "Ward 4 road crew", "dueAt": "2099-01-31"}, 201)
    wo = case["workOrders"][0]["id"]
    act(api, staff["asha"], f"/work-orders/{wo}/transitions", {"to": "In Progress", "expectedStatus": "Open"})
    return wo


def upload_repair(api, headers, wo, content):
    r = api.post(f"{API}/staff/work-orders/{wo}/evidence", files={"file": ("after.jpg", content, "image/jpeg")}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["evidenceId"]


def assessments(api, headers, cid, expect=200):
    r = api.get(f"{API}/staff/complaints/{cid}/repair-assessment", headers=headers)
    assert r.status_code == expect, r.text
    return r.json()


def rows(db_path, sql, params=()):
    with closing(dbutil.connect(db_path)) as conn:
        return conn.execute(sql, params).fetchall()


# ---------- Citizen photo -> detection -> dashboard ----------

def test_citizen_photo_is_analysed_and_shown_to_staff(api, staff, provider, isolated_db_path):
    cid, evidence_id = report(api)
    assert [c[0] for c in provider.calls] == ["pothole", "pavement"], "routed by category: pothole workflow + pavement model"
    data = analysis(api, staff["asha"], cid)
    assert data["ai"]["enabled"] and data["ai"]["provider"] == "roboflow" and data["routing"]["detectors"] == ["pothole", "pavement"]
    by = {i["detector"]: i for i in data["inferences"]}
    assert set(by) == {"pothole", "pavement"}
    for name, mechanism, model in (("pothole", "workflow", "mohammed-arshad-nb1p3/general-segmentation-api-18"),
                                   ("pavement", "model", "pavement-gvib5/1")):
        i = by[name]
        assert (i["state"], i["mechanism"], i["modelId"], i["evidenceId"], i["evidenceRole"]) == ("completed", mechanism, model, evidence_id, "original")
        assert (i["detectedCategory"], i["topConfidence"], i["detectionCount"]) == ("Potholes", 0.91, 1)
        box = i["detections"][0]["bbox"]
        assert (i["imageWidth"], i["imageHeight"]) == (1000, 750) and box["x"] == 425.0 and box["width"] == 150.0
    assert by["pavement"]["modelVersion"] == "1" and by["pothole"]["classPrompt"] == "potholes"
    # The sent image is pixels only (no EXIF), and the send is in the audit trail.
    assert all(b"Exif" not in jpeg[:100] for _, _, jpeg in provider.calls)
    audit = api.get(f"{API}/staff/complaints/{cid}/audit", headers=staff["ravi"]).json()
    sent = [a for a in audit if a["action"] == "ai_evidence_sent"]
    assert len(sent) == 2 and sent[0]["actorType"] == "system" and sent[0]["details"]["evidenceId"] == evidence_id
    # Stored as JSON in the database (JSONB on PostgreSQL).
    stored = rows(isolated_db_path, "SELECT predictions FROM ai_inferences WHERE detector = 'pavement'")[0][0]
    assert (json.loads(stored) if isinstance(stored, str) else stored)[0]["className"] == "pothole"


def test_responses_never_contain_storage_keys_or_the_api_key(api, staff):
    cid, _ = report(api)
    for path in (f"/staff/complaints/{cid}/ai", f"/staff/complaints/{cid}/repair-assessment", "/ai"):
        body = api.get(f"{API}{path}", headers=staff["asha"]).text
        assert "storageKey" not in body and "storage_key" not in body and TEST_KEY not in body


def test_public_endpoint_discloses_ai_processing_only_when_enabled(make_api, org):  # noqa: F811
    on = make_api(provider=FakeProvider()).get(f"{API}/ai").json()
    assert on["enabled"] and "Roboflow" in on["disclosure"] and "without location" in on["disclosure"]
    off = make_api(settings=AiSettings.from_env({})).get(f"{API}/ai").json()
    assert off == {"enabled": False, "disclosure": None}


def test_complaint_submission_succeeds_when_the_provider_is_down(make_api, org, isolated_db_path, evidence_dir):  # noqa: F811
    down = FakeProvider(fail_with=["provider_unavailable", "timeout"])
    api = make_api(provider=down)
    asha = login(api, "asha")
    cid, evidence_id = report(api)
    inferences = analysis(api, asha, cid)["inferences"]
    assert {(i["state"], i["errorCode"]) for i in inferences} == {("failed", "provider_unavailable"), ("failed", "timeout")}
    # The complaint and its evidence are intact and viewable.
    assert api.get(f"{API}/complaints/{cid}").status_code == 200
    assert api.get(f"{API}/staff/complaints/{cid}/evidence", headers=asha).status_code == 200


def test_retry_reruns_only_failed_jobs_without_duplicates(make_api, org, isolated_db_path):  # noqa: F811
    flaky = FakeProvider(fail_with=["rate_limited", None])
    api = make_api(provider=flaky)
    asha = login(api, "asha")
    cid, _ = report(api)
    assert sorted(i["state"] for i in analysis(api, asha, cid)["inferences"]) == ["completed", "failed"]
    r = api.post(f"{API}/staff/complaints/{cid}/ai/retry", headers=asha)
    assert r.status_code == 202 and r.json()["reset"] == 1
    states = {i["detector"]: (i["state"], i["attempts"]) for i in r.json()["inferences"]}
    assert states == {"pothole": ("completed", 2), "pavement": ("completed", 1)}
    assert len(flaky.calls) == 3, "only the failed detector was re-run"
    # Retrying again changes nothing and calls nobody.
    assert api.post(f"{API}/staff/complaints/{cid}/ai/retry", headers=asha).json()["reset"] == 0
    assert len(flaky.calls) == 3
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM ai_inferences")[0][0] == 2


def test_scheduling_twice_never_duplicates_jobs(api, staff, provider, isolated_db_path):
    cid, evidence_id = report(api)
    ai = api.app.state.ai
    ai.evidence_added(evidence_id)
    ai.evidence_added(evidence_id)
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM ai_inferences")[0][0] == 2 and len(provider.calls) == 2


def test_a_claimed_job_is_not_run_twice(api, staff, provider, isolated_db_path):
    _, evidence_id = report(api)
    ai = api.app.state.ai
    pk = rows(isolated_db_path, "SELECT pk FROM ai_inferences ORDER BY pk")[0][0]
    assert ai.db.claim_inference(pk, ai.settings.detectors["pothole"]) is None, "already completed"


def test_jobs_left_pending_are_resumed_at_startup(make_api, org, isolated_db_path):  # noqa: F811
    stalled = NullExecutor()
    first = make_api(provider=FakeProvider(), executor=stalled)
    cid, _ = report(first)
    assert stalled.jobs and {r[0] for r in rows(isolated_db_path, "SELECT state FROM ai_inferences")} == {"pending"}
    first.__exit__(None, None, None)
    second = make_api(provider=(p := FakeProvider()))
    assert {r[0] for r in rows(isolated_db_path, "SELECT state FROM ai_inferences")} == {"completed"} and len(p.calls) == 2


def test_videos_are_never_sent_as_images(api, staff, provider):
    cid, _ = report(api, content=MP4, media_type="video")
    inferences = analysis(api, staff["asha"], cid)["inferences"]
    assert {(i["state"], i["errorCode"]) for i in inferences} == {("skipped", "unsupported_media")}
    assert provider.calls == []


def test_undecodable_photo_fails_cleanly(api, staff, provider):
    cid, _ = report(api, content=b"\xff\xd8\xff\xe0" + b"\x00" * 3000 + b"\xff\xd9")
    assert {(i["state"], i["errorCode"]) for i in analysis(api, staff["asha"], cid)["inferences"]} == {("failed", "invalid_image")}
    assert provider.calls == []


def test_categories_without_a_detector_are_left_to_people(api, staff, provider):
    cid, _ = report(api, category="Water leakage")
    data = analysis(api, staff["asha"], cid)
    assert data["inferences"] == [] and data["routing"]["detectors"] == [] and provider.calls == []


def test_garbage_complaints_use_the_garbage_workflow(api, staff, provider):
    cid, _ = report(api, category="Illegal dumping")
    i = analysis(api, staff["asha"], cid)["inferences"]
    assert [x["detector"] for x in i] == ["garbage"] and i[0]["detectedCategory"] == "Garbage accumulation"


def test_ai_disabled_or_unconfigured(make_api, org):  # noqa: F811
    for settings, reason in ((AiSettings.from_env({}), "disabled"),
                             (AiSettings.from_env({"CIVICLENS_AI_PROVIDER": "roboflow"}), "not_configured")):
        api = make_api(settings=settings)
        asha = login(api, "asha")
        cid, _ = report(api)  # submission works regardless
        data = analysis(api, asha, cid)
        assert data["ai"] == {"enabled": False, "provider": None if reason == "disabled" else "roboflow", "reason": reason,
                              "disclosure": None, "thresholds": data["ai"]["thresholds"]}
        assert data["inferences"] == []
        r = api.post(f"{API}/staff/complaints/{cid}/ai/retry", headers=asha)
        assert r.status_code == 503 and r.json()["error"]["code"] == "ai_unavailable"


# ---------- Authorisation ----------

def test_staff_authorisation_and_department_scope(api, staff):
    cid, _ = report(api)
    assign(api, staff["ravi"], cid)  # roads
    paths = [("get", f"/staff/complaints/{cid}/ai"), ("post", f"/staff/complaints/{cid}/ai/retry"),
             ("post", f"/staff/complaints/{cid}/verify-repair"), ("get", f"/staff/complaints/{cid}/repair-assessment")]
    for method, path in paths:
        assert getattr(api, method)(f"{API}{path}").status_code == 401
        assert getattr(api, method)(f"{API}{path}", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
        assert getattr(api, method)(f"{API}{path}", headers=staff["bilal"]).status_code == 403, path  # waste official
    assert analysis(api, staff["asha"], cid)["complaintId"] == cid
    assert analysis(api, staff["ravi"], cid)["complaintId"] == cid
    assert api.get(f"{API}/staff/complaints/NCL-2026-ZZZZZZ/ai", headers=staff["ravi"]).status_code == 404
    assert api.get(f"{API}/staff/complaints/not-an-id/ai", headers=staff["ravi"]).status_code == 404


# ---------- Repair verification ----------

def test_apparent_repair_is_suggested_and_needs_a_supervisor(api, staff, provider, isolated_db_path):
    cid, original_id = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    repair_id = upload_repair(api, staff["asha"], wo, photo(1, "clear", moved=True, gps=(13.10075, 77.59635)))
    data = assessments(api, staff["asha"], cid)
    assert len(data["assessments"]) == 1
    a = data["assessments"][0]
    assert (a["state"], a["suggestedOutcome"], a["locationCheck"]) == ("completed", "likely_repaired", "match")
    assert (a["originalEvidenceId"], a["repairEvidenceId"], a["workOrderId"]) == (original_id, repair_id, wo)
    assert a["gpsDistanceM"] < 10 and 0 < a["verificationConfidence"] < 1
    assert a["review"]["status"] == "pending_review" and a["verifiedResolution"] == "unverified"
    links = rows(isolated_db_path, "SELECT phase, COUNT(*) FROM repair_assessment_inferences GROUP BY phase ORDER BY phase")
    assert [tuple(r) for r in links] == [("original", 2), ("repair", 2)]

    # The repair photo has its own inference results, linked to the repair evidence.
    repair_results = [i for i in analysis(api, staff["asha"], cid)["inferences"] if i["evidenceRole"] == "repair"]
    assert {i["evidenceId"] for i in repair_results} == {repair_id} and all(i["detectionCount"] == 0 for i in repair_results)

    status_before = api.get(f"{API}/staff/complaints/{cid}", headers=staff["ravi"]).json()["status"]
    review = f"{API}/staff/complaints/{cid}/repair-assessment/{a['id']}/review"
    assert api.post(review, json={"decision": "approved"}, headers=staff["asha"]).status_code == 403, "officials cannot review"
    r = api.post(review, json={"decision": "approved", "note": "Matches site photos."}, headers=staff["ravi"])
    assert r.status_code == 200, r.text
    assert r.json()["review"]["status"] == "approved" and r.json()["review"]["reviewer"] == "ravi"
    assert r.json()["verifiedResolution"] == "verified_repaired"
    assert api.post(review, json={"decision": "rejected", "note": "x"}, headers=staff["meera"]).status_code == 409
    # Administrative status is untouched by AI and by the review.
    case = api.get(f"{API}/staff/complaints/{cid}", headers=staff["ravi"]).json()
    assert case["status"] == status_before == "In Progress" and case["verification"] == "Pending Verification"
    actions = [x["action"] for x in api.get(f"{API}/staff/complaints/{cid}/audit", headers=staff["ravi"]).json()]
    assert "repair_assessment_completed" in actions and "repair_assessment_reviewed" in actions


def test_damage_still_visible(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["asha"], wo, photo(1, "damage", moved=True))
    a = assessments(api, staff["asha"], cid)["assessments"][0]
    assert a["suggestedOutcome"] == "damage_visible" and a["details"]["overlapsOriginalDamage"] is True


def test_wrong_location_photo_is_inconclusive(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["asha"], wo, photo(7, "clear", gps=(13.13, 77.62)))  # another street, ~4 km away
    a = assessments(api, staff["asha"], cid)["assessments"][0]
    assert (a["suggestedOutcome"], a["locationCheck"]) == ("inconclusive", "mismatch") and a["gpsDistanceM"] > 3000
    assert a["verificationConfidence"] is None


def test_unmatched_photo_without_gps_is_inconclusive(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["asha"], wo, photo(7, "clear"))
    a = assessments(api, staff["asha"], cid)["assessments"][0]
    assert (a["suggestedOutcome"], a["locationCheck"]) == ("inconclusive", "unverified")


def test_low_quality_after_photo_is_inconclusive(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["asha"], wo, photo(1, "clear", moved=True, dark=True))
    a = assessments(api, staff["asha"], cid)["assessments"][0]
    assert a["suggestedOutcome"] == "inconclusive"
    assert "too_dark" in a["details"]["quality"]["repair"]["problems"] or a["locationCheck"] != "match"


def test_unclear_detection_is_inconclusive(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["asha"], wo, photo(1, "unclear", moved=True))
    assert assessments(api, staff["asha"], cid)["assessments"][0]["suggestedOutcome"] == "inconclusive"


def test_verify_repair_endpoint_is_idempotent_and_validates_input(api, staff, isolated_db_path):
    cid, _ = report(api, content=photo(1, "damage"))
    url = f"{API}/staff/complaints/{cid}/verify-repair"
    r = api.post(url, headers=staff["asha"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_repair_evidence"
    wo = work_order(api, staff, cid)
    repair_id = upload_repair(api, staff["asha"], wo, photo(1, "clear", moved=True))
    for body in (None, {"repairEvidenceId": repair_id}):
        r = api.post(url, json=body, headers=staff["asha"])
        assert r.status_code == 202, r.text
        assert len(r.json()["assessments"]) == 1, "the same pair is never assessed twice"
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM repair_assessments")[0][0] == 1
    assert api.post(url, json={"repairEvidenceId": "ev_" + "x" * 22}, headers=staff["asha"]).status_code == 404
    assert api.post(url, json={"repairEvidenceId": "../etc"}, headers=staff["asha"]).status_code == 404
    assert api.post(url, json={"unexpected": 1}, headers=staff["asha"]).status_code == 422


def test_repair_check_needs_a_photo_original(api, staff):
    cid, _ = report(api, content=MP4, media_type="video")
    r = api.post(f"{API}/staff/complaints/{cid}/verify-repair", headers=staff["asha"])
    assert r.status_code == 422 and r.json()["error"]["code"] == "unsupported_media"


def test_uploader_cannot_review_and_rejection_needs_a_reason(api, staff):
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    upload_repair(api, staff["ravi"], wo, photo(1, "clear", moved=True))  # the supervisor took the after photo
    a = assessments(api, staff["ravi"], cid)["assessments"][0]
    review = f"{API}/staff/complaints/{cid}/repair-assessment/{a['id']}/review"
    assert api.post(review, json={"decision": "approved"}, headers=staff["ravi"]).status_code == 403
    r = api.post(review, json={"decision": "rejected", "note": "  "}, headers=staff["meera"])
    assert r.status_code == 422
    r = api.post(review, json={"decision": "rejected", "note": "Shadow hides the patch; site visit needed."}, headers=staff["meera"])
    assert r.status_code == 200 and r.json()["verifiedResolution"] == "unverified" and r.json()["review"]["status"] == "rejected"
    assert api.post(f"{API}/staff/complaints/{cid}/repair-assessment/ra_bad/review", json={"decision": "approved"},
                    headers=staff["meera"]).status_code == 404


def test_original_and_repair_evidence_are_both_preserved(api, staff, isolated_db_path):
    cid, original_id = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    first = upload_repair(api, staff["asha"], wo, photo(1, "damage", moved=True))
    second = upload_repair(api, staff["asha"], wo, photo(1, "clear", moved=True))
    ids = {r[0] for r in rows(isolated_db_path, "SELECT evidence_id FROM evidence")}
    assert {original_id, first, second} <= ids
    outcomes = [(a["repairEvidenceId"], a["suggestedOutcome"]) for a in assessments(api, staff["asha"], cid)["assessments"]]
    assert outcomes == [(second, "likely_repaired"), (first, "damage_visible")], "newest first; each repair photo assessed once"


def test_failed_repair_inference_makes_assessment_inconclusive_and_retry_recovers(make_api, org):  # noqa: F811
    p = FakeProvider()
    api = make_api(provider=p)
    staff = {u: login(api, u) for u in ("asha", "ravi")}
    cid, _ = report(api, content=photo(1, "damage"))
    wo = work_order(api, staff, cid)
    p.fail_with = ["auth_failed", "auth_failed"]
    upload_repair(api, staff["asha"], wo, photo(1, "clear", moved=True))
    a = assessments(api, staff["asha"], cid)["assessments"][0]
    assert (a["state"], a["errorCode"], a["suggestedOutcome"]) == ("failed", "inference_failed", None), \
        "a failed analysis is not a judgement about the repair"
    r = api.post(f"{API}/staff/complaints/{cid}/ai/retry", headers=staff["asha"])
    assert r.status_code == 202 and r.json()["reset"] == 3, "two inferences and the assessment"
    after = assessments(api, staff["asha"], cid)["assessments"]
    assert len(after) == 1 and after[0]["id"] == a["id"]
    assert (after[0]["state"], after[0]["suggestedOutcome"]) == ("completed", "likely_repaired")
