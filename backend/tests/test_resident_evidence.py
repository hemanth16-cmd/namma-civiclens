"""A resident sees the evidence of their own complaints (My Complaints), and nobody else's.

The owner gets evidence metadata in GET /residents/me/complaints(/{id}) and streams each file from
GET /residents/me/complaints/{id}/evidence/{evidenceId} with their session; the bucket stays private
and no storage key or URL is ever returned."""

import json

from tests.test_complaints import payload
from tests.test_evidence import JPEG, WEBM
from tests.test_residents import API, api, bearer, make_api, resident_token  # noqa: F401 (fixtures)


def report(api, token=None, media=("e.jpg", JPEG, "image/jpeg"), **overrides):
    r = api.post(f"{API}/complaints", json=payload(**overrides), headers=bearer(token) if token else {})
    assert r.status_code == 201, r.text
    receipt = r.json()
    up = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": media}, headers=bearer(receipt["uploadToken"]))
    assert up.status_code == 201, up.text
    return receipt, up.json()


def my_list(api, token):
    r = api.get(f"{API}/residents/me/complaints", headers=bearer(token))
    assert r.status_code == 200, r.text
    return {i["id"]: i for i in r.json()["items"]}


def evidence_url(cid, eid):
    return f"{API}/residents/me/complaints/{cid}/evidence/{eid}"


def test_my_complaints_list_their_uploaded_photo_and_stream_it_to_the_owner(api):
    asha = resident_token(api)
    receipt, upload = report(api, asha)
    item = my_list(api, asha)[receipt["id"]]
    assert item["hasEvidence"] is True
    assert item["evidence"] == [{"evidenceId": upload["evidenceId"], "role": "original", "mediaType": "photo", "contentType": "image/jpeg",
                                 "byteSize": len(JPEG), "capturedAt": None, "uploadedAt": upload["uploadedAt"]}]
    detail = api.get(f"{API}/residents/me/complaints/{receipt['id']}", headers=bearer(asha)).json()
    assert detail["evidence"] == item["evidence"]
    got = api.get(evidence_url(receipt["id"], upload["evidenceId"]), headers=bearer(asha))
    assert got.status_code == 200 and got.content == JPEG
    assert got.headers["content-type"] == "image/jpeg" and "no-store" in got.headers["cache-control"]
    assert got.headers["x-content-type-options"] == "nosniff"
    raw = json.dumps(item)
    for private in ("storageKey", "storage_key", "storageBackend", "supabase", "/objects/", "http"):
        assert private not in raw, private


def test_videos_are_listed_and_streamed_as_video(api):
    asha = resident_token(api)
    receipt, upload = report(api, asha, media=("v.webm", WEBM, "video/webm"), mediaType="video")
    ev = my_list(api, asha)[receipt["id"]]["evidence"][0]
    assert ev["mediaType"] == "video" and ev["contentType"] == "video/webm"
    got = api.get(evidence_url(receipt["id"], upload["evidenceId"]), headers=bearer(asha))
    assert got.status_code == 200 and got.headers["content-type"] == "video/webm" and got.content == WEBM


def test_another_residents_evidence_is_not_reachable(api):
    asha = resident_token(api)
    bina = resident_token(api, "bina@example.org", "Bina")
    a_receipt, a_upload = report(api, asha)
    b_receipt, _ = report(api, bina)
    assert set(my_list(api, bina)) == {b_receipt["id"]}
    assert set(my_list(api, asha)) == {a_receipt["id"]}
    for url, headers in ((evidence_url(a_receipt["id"], a_upload["evidenceId"]), bearer(bina)),   # B asks for A's file
                         (evidence_url(b_receipt["id"], a_upload["evidenceId"]), bearer(bina)),   # A's file under B's complaint
                         (evidence_url(a_receipt["id"], "ev_" + "x" * 22), bearer(asha)),       # no such file
                         (evidence_url(a_receipt["id"], "not-an-id"), bearer(asha))):
        r = api.get(url, headers=headers)
        assert r.status_code == 404 and JPEG not in r.content, url
    assert api.get(evidence_url(a_receipt["id"], a_upload["evidenceId"])).status_code == 401
    assert api.get(evidence_url(a_receipt["id"], a_upload["evidenceId"]), headers=bearer(a_receipt["uploadToken"])).status_code == 401


def test_anonymous_reports_are_nobodys_until_claimed(api):
    asha = resident_token(api)
    receipt, upload = report(api)  # no session: anonymous
    assert receipt["id"] not in my_list(api, asha)
    assert api.get(evidence_url(receipt["id"], upload["evidenceId"]), headers=bearer(asha)).status_code == 404
    assert api.post(f"{API}/residents/me/claims", json={"id": receipt["id"], "claim": receipt["claim"]}, headers=bearer(asha)).status_code == 200
    assert my_list(api, asha)[receipt["id"]]["evidence"][0]["evidenceId"] == upload["evidenceId"]
    assert api.get(evidence_url(receipt["id"], upload["evidenceId"]), headers=bearer(asha)).content == JPEG


def test_after_repair_photos_join_the_owners_evidence(api):
    store = api.app.state.store
    asha = resident_token(api)
    receipt, upload = report(api, asha)
    store.create_staff("ravi", "Ravi", "scrypt$x", role="supervisor")
    ravi = store.get_staff("ravi")["pk"]
    store.create_department("roads", "Road maintenance")
    store.assign(receipt["id"], ravi, "roads", None, "", "Submitted")
    order = store.create_work_order(receipt["id"], ravi, "Ward crew", "", None, None)
    repair = {"evidenceId": "ev_" + "r" * 22, "storageKey": "ab" * 16, "mediaType": "photo", "contentType": "image/jpeg", "byteSize": 9,
              "sha256": "0" * 64, "capturedAt": None, "uploadedAt": "2026-10-31T09:00:00.000Z", "storageBackend": "local"}
    store.add_repair_evidence(order, ravi, repair)
    evidence = my_list(api, asha)[receipt["id"]]["evidence"]
    assert [(e["evidenceId"], e["role"]) for e in evidence] == [(upload["evidenceId"], "original"), (repair["evidenceId"], "repair")]
    # The repair row points at a file that was never written: the owner gets a clear 404, not a crash or a 503.
    missing = api.get(evidence_url(receipt["id"], repair["evidenceId"]), headers=bearer(asha))
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "evidence_unavailable"


def test_public_endpoints_never_expose_evidence(api):
    asha = resident_token(api)
    receipt, upload = report(api, asha)
    for url in (f"{API}/complaints/{receipt['id']}", f"{API}/complaints"):
        body = api.get(url).text
        assert upload["evidenceId"] not in body and "evidence" not in body.lower().replace("evidencecount", ""), url
