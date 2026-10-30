"""Web Push notifications: inboxes, follows, device subscriptions, history, events and delivery.

Delivery through the app uses a fake PushSender (no network). WebPushSender itself is tested
against a fake push service that captures the HTTP request, which is then decrypted with the
subscriber's private key and its VAPID signature verified, so the real encryption and signing run.
"""

import base64
import json
import os
import time
import uuid
from contextlib import closing
from dataclasses import replace

import http_ece
import pytest
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi.testclient import TestClient

from app import notifications as notifications_module
from app.main import create_app
from app.push import (DEFAULT_ENDPOINT_HOSTS, PushConfigError, PushResult, PushSettings, WebPushSender, b64url_decode,
                      b64url_encode, generate_vapid_keys, main as push_cli)
from app.ratelimit import InMemoryRateLimiter, RateLimitSettings, Rule
from app.store import ComplaintStore, StoreError
from tests import dbutil
from tests.test_complaints import payload
from tests.test_evidence import JPEG
from tests.test_staff import PASSWORD, run_cli

PUBLIC, PRIVATE = generate_vapid_keys()
SETTINGS = PushSettings(PUBLIC, PRIVATE, "mailto:ops@example.org")
API = "/api/v1"


class FakeSender:
    """Records every push; `script[endpoint]` lists outcomes to return in order (default "sent")."""

    STATUS = {"sent": 201, "gone": 410, "retry": 503, "rejected": 400}

    def __init__(self):
        self.sent = []
        self.script = {}

    def send(self, device, payload):
        self.sent.append((device["endpoint"], json.loads(payload)))
        outcomes = self.script.get(device["endpoint"])
        outcome = outcomes.pop(0) if outcomes else "sent"
        return PushResult(outcome, self.STATUS[outcome])

    def to(self, endpoint):
        return [p for e, p in self.sent if e == endpoint]


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def subscription(host="fcm.googleapis.com", private_key=None, auth=None):
    key = private_key or ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": f"https://{host}/fcm/send/{uuid.uuid4().hex}",
            "keys": {"p256dh": b64url_encode(public), "auth": b64url_encode(auth or os.urandom(16))}}


def rows(db, sql, params=()):
    with closing(dbutil.connect(db)) as conn:
        return conn.execute(sql, params).fetchall()


@pytest.fixture
def sender():
    return FakeSender()


@pytest.fixture
def make_api(isolated_db_path, evidence_dir, sender):
    clients = []

    def make(**kwargs):
        kwargs.setdefault("push_settings", SETTINGS)
        kwargs.setdefault("push_sender", sender)
        client = TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, **kwargs), client=("10.0.0.9", 5000))
        client.__enter__()
        clients.append(client)
        return client

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    return make_api()


@pytest.fixture
def staff(isolated_db_path, monkeypatch, api):
    """Two staff accounts (asha, ravi); returns their session tokens."""
    tokens = {}
    for name in ("asha", "ravi"):
        assert run_cli(isolated_db_path, "create", "--username", name, "--display-name", name.title(), "--password-stdin",
                       stdin=PASSWORD, monkeypatch=monkeypatch) == 0
        tokens[name] = api.post(f"{API}/staff/sessions", json={"username": name, "password": PASSWORD}).json()["token"]
    return tokens


def new_inbox(api):
    response = api.post(f"{API}/notifications/inboxes")
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    return response.json()["inboxToken"]


def new_complaint(api, upload=False):
    receipt = api.post(f"{API}/complaints", json=payload()).json()
    if upload:
        upload_evidence(api, receipt)
    return receipt


def upload_evidence(api, receipt):
    response = api.post(f"{API}/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")},
                        headers=bearer(receipt["uploadToken"]))
    assert response.status_code == 201, response.text


def follow(api, token, receipt, claim=None):
    return api.post(f"{API}/notifications/follows", json={"complaintId": receipt["id"], "claim": claim or receipt["claim"]},
                    headers=bearer(token))


def register(api, token, sub, label=""):
    return api.post(f"{API}/notifications/subscriptions", json={**sub, "label": label}, headers=bearer(token))


def history(api, token, **params):
    response = api.get(f"{API}/notifications", params=params, headers=bearer(token))
    assert response.status_code == 200, response.text
    return response.json()


# ---------- Configuration ----------

def test_config_reports_push_state_and_public_key_only(make_api):
    on = make_api().get(f"{API}/notifications/config").json()
    assert on == {"pushEnabled": True, "vapidPublicKey": PUBLIC}
    assert PRIVATE not in json.dumps(on)


def test_push_disabled_without_keys(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as api:
        assert api.get(f"{API}/notifications/config").json() == {"pushEnabled": False, "vapidPublicKey": None}


def test_push_settings_validation(monkeypatch):
    other_public, _ = generate_vapid_keys()
    for bad, needle in [(dict(public_key=other_public), "does not belong"), (dict(subject="ops@example.org"), "VAPID_SUBJECT"),
                        (dict(private_key="short"), "VAPID_PRIVATE_KEY"), (dict(public_key="AAAA"), "VAPID_PUBLIC_KEY"),
                        (dict(ttl=-1), "CIVICLENS_PUSH_TTL_SECONDS")]:
        with pytest.raises(PushConfigError, match=needle) as caught:
            replace(SETTINGS, **bad)
        assert PRIVATE not in str(caught.value)
    assert PRIVATE not in repr(SETTINGS)
    monkeypatch.setenv("VAPID_PUBLIC_KEY", PUBLIC)
    with pytest.raises(PushConfigError, match="both"):
        PushSettings.from_env()
    monkeypatch.setenv("VAPID_PRIVATE_KEY", PRIVATE)
    monkeypatch.setenv("VAPID_SUBJECT", "https://civiclens.example.org")
    monkeypatch.setenv("CIVICLENS_PUSH_ENDPOINT_HOSTS", "push.example.net, fcm.googleapis.com")
    loaded = PushSettings.from_env()
    assert loaded.endpoint_hosts == ("push.example.net", "fcm.googleapis.com") and loaded.ttl == 24 * 3600
    for name in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"):
        monkeypatch.delenv(name)
    assert PushSettings.from_env() is None


def test_misconfigured_push_stops_the_app_from_building(monkeypatch, isolated_db_path):
    monkeypatch.setenv("VAPID_PUBLIC_KEY", PUBLIC)
    with pytest.raises(PushConfigError):
        create_app(db_path=isolated_db_path)


def test_cli_generates_a_matching_key_pair(capsys):
    assert push_cli(["generate-vapid-keys"]) == 0
    lines = dict(line.split("=", 1) for line in capsys.readouterr().out.splitlines() if "=" in line and not line.startswith("#"))
    PushSettings(lines["VAPID_PUBLIC_KEY"], lines["VAPID_PRIVATE_KEY"], "mailto:a@b.example")  # validates the pair
    assert push_cli([]) == 2


# ---------- Authorisation ----------

def test_notification_routes_need_a_staff_session_or_inbox_token(api, staff):
    for method, path in [("GET", ""), ("GET", "/unread-count"), ("POST", "/read-all"), ("GET", "/subscriptions"),
                         ("GET", "/follows"), ("POST", "/test")]:
        assert api.request(method, f"{API}/notifications{path}").status_code == 401
        assert api.request(method, f"{API}/notifications{path}", headers=bearer("x" * 43)).status_code == 401
    assert api.get(f"{API}/notifications", headers=bearer(staff["asha"])).status_code == 200
    assert api.get(f"{API}/notifications", headers=bearer(new_inbox(api))).status_code == 200


def test_inbox_tokens_are_stored_hashed_and_do_not_open_staff_routes(api, isolated_db_path):
    token = new_inbox(api)
    [(stored,)] = rows(isolated_db_path, "SELECT token_hash FROM notification_recipients WHERE kind = 'citizen'")
    assert token not in stored and len(stored) == 64
    assert api.get(f"{API}/staff/complaints", headers=bearer(token)).status_code == 401


def test_revoked_staff_session_loses_access(api, staff):
    api.delete(f"{API}/staff/sessions/current", headers=bearer(staff["asha"]))
    assert api.get(f"{API}/notifications", headers=bearer(staff["asha"])).status_code == 401


def test_inbox_creation_is_rate_limited(make_api):
    api = make_api(rate_limit_settings=replace(RateLimitSettings(), inbox_per_ip=Rule(2, 3600)), rate_limiter=InMemoryRateLimiter())
    assert [api.post(f"{API}/notifications/inboxes").status_code for _ in range(3)] == [201, 201, 429]


# ---------- Follows ----------

def test_follow_needs_the_claim_code_and_confirms_once(api, sender):
    receipt, token = new_complaint(api), new_inbox(api)
    assert follow(api, token, receipt, claim="WRONG-CODE").status_code == 401
    assert api.post(f"{API}/notifications/follows", json={"complaintId": "NCL-2026-ZZZZZZ", "claim": "x"}, headers=bearer(token)).status_code == 404
    first = follow(api, token, receipt)
    assert first.status_code == 201 and first.json()["complaintId"] == receipt["id"]
    assert follow(api, token, receipt).status_code == 200, "following again is not an error"
    items = history(api, token)["items"]
    assert [i["kind"] for i in items] == ["follow_confirmed"], "one confirmation only"
    assert items[0]["complaintId"] == receipt["id"] and items[0]["url"] == f"#/track/{receipt['id']}"
    assert api.get(f"{API}/notifications/follows", headers=bearer(token)).json()[0]["complaintId"] == receipt["id"]
    assert api.delete(f"{API}/notifications/follows/{receipt['id']}", headers=bearer(token)).status_code == 204
    assert api.delete(f"{API}/notifications/follows/{receipt['id']}", headers=bearer(token)).status_code == 404


def test_staff_cannot_follow(api, staff):
    assert follow(api, staff["asha"], new_complaint(api)).status_code == 403


def test_follow_limit(api, monkeypatch):
    monkeypatch.setattr("app.notifications_api.MAX_FOLLOWS", 2)
    token = new_inbox(api)
    assert [follow(api, token, new_complaint(api)).status_code for _ in range(3)] == [201, 201, 409]


def test_follow_attempts_are_rate_limited(make_api):
    api = make_api(rate_limit_settings=replace(RateLimitSettings(), follow_per_ip=Rule(2, 900)), rate_limiter=InMemoryRateLimiter())
    receipt, token = new_complaint(api), new_inbox(api)
    codes = [follow(api, token, receipt, claim="WRONG").status_code for _ in range(3)]
    assert codes == [401, 401, 429]


# ---------- Devices ----------

def test_register_list_and_remove_devices_without_exposing_endpoints(api, isolated_db_path):
    token = new_inbox(api)
    sub = subscription()
    created = register(api, token, sub, label="Chrome on Android")
    assert created.status_code == 201
    device = created.json()
    assert device["subscriptionId"].startswith("ps_") and device["label"] == "Chrome on Android"
    again = register(api, token, sub)
    assert again.status_code == 200 and again.json()["subscriptionId"] == device["subscriptionId"], "no duplicate device"
    listed = api.get(f"{API}/notifications/subscriptions", headers=bearer(token))
    assert len(listed.json()) == 1
    assert sub["endpoint"] not in listed.text and sub["keys"]["auth"] not in listed.text
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM push_subscriptions")[0][0] == 1
    assert api.delete(f"{API}/notifications/subscriptions/{device['subscriptionId']}", headers=bearer(token)).status_code == 204
    assert api.get(f"{API}/notifications/subscriptions", headers=bearer(token)).json() == []


def test_devices_belong_to_their_owner(api, staff):
    alice, bob = new_inbox(api), new_inbox(api)
    device = register(api, alice, subscription()).json()
    assert api.delete(f"{API}/notifications/subscriptions/{device['subscriptionId']}", headers=bearer(bob)).status_code == 404
    assert api.delete(f"{API}/notifications/subscriptions/{device['subscriptionId']}", headers=bearer(staff["asha"])).status_code == 404
    assert api.get(f"{API}/notifications/subscriptions", headers=bearer(bob)).json() == []
    assert len(api.get(f"{API}/notifications/subscriptions", headers=bearer(alice)).json()) == 1


def test_one_browser_can_register_for_citizen_and_staff(api, staff):
    sub = subscription()
    assert register(api, new_inbox(api), sub).status_code == 201
    assert register(api, staff["asha"], sub).status_code == 201


@pytest.mark.parametrize("change, code", [
    (lambda s: {**s, "endpoint": s["endpoint"].replace("https://", "http://")}, "invalid_subscription"),
    (lambda s: {**s, "endpoint": "https://127.0.0.1/push/1"}, "unsupported_push_service"),
    (lambda s: {**s, "endpoint": "https://fcm.googleapis.com.evil.example/x"}, "unsupported_push_service"),
    (lambda s: {**s, "endpoint": "https://fcm.googleapis.com:8443/x"}, "invalid_subscription"),
    (lambda s: {**s, "endpoint": "https://user:pw@fcm.googleapis.com/x"}, "invalid_subscription"),
    (lambda s: {**s, "keys": {**s["keys"], "auth": b64url_encode(os.urandom(12))}}, "invalid_subscription"),
    (lambda s: {**s, "keys": {**s["keys"], "p256dh": b64url_encode(b"\x05" + os.urandom(64))}}, "invalid_subscription"),
    (lambda s: {**s, "label": "<script>"}, "invalid_subscription"),
])
def test_invalid_subscriptions_are_refused(api, change, code):
    response = api.post(f"{API}/notifications/subscriptions", json=change(subscription()), headers=bearer(new_inbox(api)))
    assert response.status_code == 422 and response.json()["error"]["code"] == code


def test_known_push_services_are_accepted(api):
    token = new_inbox(api)
    for host in ("fcm.googleapis.com", "updates.push.services.mozilla.com", "wns2-pn1p.notify.windows.com", "web.push.apple.com"):
        assert register(api, token, subscription(host=host)).status_code == 201, host


def test_device_limit(api, monkeypatch):
    monkeypatch.setattr("app.notifications_api.MAX_DEVICES", 2)
    token = new_inbox(api)
    assert [register(api, token, subscription()).status_code for _ in range(3)] == [201, 201, 409]


# ---------- Events, history and read state ----------

def test_citizen_and_staff_receive_complaint_events(api, staff, sender):
    citizen = new_inbox(api)
    phone, laptop, staff_phone = subscription(), subscription(), subscription()
    register(api, citizen, phone)
    register(api, citizen, laptop)
    register(api, staff["asha"], staff_phone)
    receipt = new_complaint(api)
    follow(api, citizen, receipt)
    upload_evidence(api, receipt)
    for _ in range(2):  # staff open the evidence twice: the citizen is told once
        assert api.get(f"{API}/staff/complaints/{receipt['id']}/evidence", headers=bearer(staff["asha"])).status_code == 200

    citizen_kinds = [i["kind"] for i in history(api, citizen)["items"]]
    assert citizen_kinds == ["evidence_reviewed", "evidence_received", "follow_confirmed"]
    for name in ("asha", "ravi"):
        assert [i["kind"] for i in history(api, staff[name])["items"]] == ["new_evidence", "new_complaint"]
    # Every citizen device got every citizen notification; the staff device got the staff ones.
    assert [p["kind"] for p in sender.to(phone["endpoint"])] == ["follow_confirmed", "evidence_received", "evidence_reviewed"]
    assert sender.to(laptop["endpoint"]) == sender.to(phone["endpoint"])
    assert [p["kind"] for p in sender.to(staff_phone["endpoint"])] == ["new_complaint", "new_evidence"]
    pushed = sender.to(staff_phone["endpoint"])[0]
    assert pushed["audience"] == "staff" and pushed["url"] == f"#/staff/{receipt['id']}" and pushed["id"].startswith("nt_")
    assert history(api, staff["asha"])["items"][-1]["pushStatus"] == "sent"
    assert history(api, staff["ravi"])["items"][-1]["pushStatus"] == "no_devices"


def test_push_payloads_carry_no_private_report_data(api, staff, sender):
    citizen = new_inbox(api)
    register(api, citizen, subscription())
    register(api, staff["asha"], subscription())
    receipt = new_complaint(api)
    follow(api, citizen, receipt)
    upload_evidence(api, receipt)
    blob = json.dumps(sender.sent)
    for secret in (receipt["claim"], receipt["uploadToken"], payload()["description"], "13.", "77."):
        assert secret not in blob


def test_disabled_staff_get_no_notifications(api, staff, isolated_db_path, monkeypatch):
    assert run_cli(isolated_db_path, "disable", "--username", "ravi") == 0
    new_complaint(api)
    assert rows(isolated_db_path, "SELECT COUNT(*) FROM notifications n JOIN notification_recipients r ON r.pk = n.recipient_pk "
                                  "JOIN staff_accounts a ON a.pk = r.staff_pk WHERE a.username = 'ravi'")[0][0] == 0
    assert len(history(api, staff["asha"])["items"]) == 1


def test_idempotent_replay_does_not_notify_twice(api, staff):
    headers, body = {"Idempotency-Key": "k" * 32}, payload()
    assert api.post(f"{API}/complaints", json=body, headers=headers).status_code == 201
    assert api.post(f"{API}/complaints", json=body, headers=headers).status_code == 200
    assert len(history(api, staff["asha"])["items"]) == 1


def test_read_unread_counts_and_paging(api, staff):
    token = staff["asha"]
    for _ in range(5):
        new_complaint(api)
    page = history(api, token, limit=2)
    assert page["unreadCount"] == 5 and len(page["items"]) == 2 and page["nextBefore"] == page["items"][-1]["id"]
    rest = history(api, token, limit=10, before=page["nextBefore"])
    assert len(rest["items"]) == 3 and rest["nextBefore"] is None
    assert not {i["id"] for i in page["items"]} & {i["id"] for i in rest["items"]}
    first = page["items"][0]["id"]
    read = api.post(f"{API}/notifications/{first}/read", headers=bearer(token)).json()
    assert read["read"] and read["readAt"]
    again = api.post(f"{API}/notifications/{first}/read", headers=bearer(token)).json()
    assert again["readAt"] == read["readAt"], "the first read time is kept"
    assert api.get(f"{API}/notifications/unread-count", headers=bearer(token)).json() == {"unreadCount": 4}
    assert [i["id"] for i in history(api, token, unread="true")["items"]] == [i["id"] for i in page["items"][1:] + rest["items"]]
    assert api.post(f"{API}/notifications/{first}/unread", headers=bearer(token)).json()["read"] is False
    assert api.post(f"{API}/notifications/read-all", headers=bearer(token)).json() == {"updated": 5, "unreadCount": 0}
    assert history(api, token)["unreadCount"] == 0
    assert history(api, staff["ravi"])["unreadCount"] == 5, "read state is per recipient"


def test_notifications_belong_to_their_recipient(api, staff):
    new_complaint(api)
    asha_item = history(api, staff["asha"])["items"][0]["id"]
    citizen = new_inbox(api)
    for path in (f"/{asha_item}/read", f"/{asha_item}/unread"):
        assert api.post(f"{API}/notifications{path}", headers=bearer(staff["ravi"])).status_code == 404
        assert api.post(f"{API}/notifications{path}", headers=bearer(citizen)).status_code == 404
    assert api.post(f"{API}/notifications/nt_not-a-real-id-000/read", headers=bearer(staff["asha"])).status_code == 404
    assert history(api, citizen)["items"] == []
    assert history(api, staff["ravi"], before=asha_item)["items"] == [], "another recipient's cursor matches nothing"
    assert history(api, staff["asha"])["unreadCount"] == 1


def test_test_notification_reaches_all_of_my_devices(api, staff, sender):
    token = new_inbox(api)
    a, b = subscription(), subscription()
    register(api, token, a)
    register(api, token, b)
    assert api.post(f"{API}/notifications/test", headers=bearer(token)).json() == {"status": "queued", "pushEnabled": True}
    assert [p["kind"] for p in sender.to(a["endpoint"])] == ["test"] == [p["kind"] for p in sender.to(b["endpoint"])]
    item = history(api, token)["items"][0]
    assert item["kind"] == "test" and item["url"] == "#/notifications" and item["pushStatus"] == "sent"
    api.post(f"{API}/notifications/test", headers=bearer(staff["asha"]))
    assert history(api, staff["asha"])["items"][0]["url"] == "#/staff/notifications"


# ---------- Delivery failures and cleanup ----------

def test_gone_subscriptions_are_deleted_and_others_still_delivered(api, sender, isolated_db_path):
    token = new_inbox(api)
    expired, healthy = subscription(), subscription()
    register(api, token, expired)
    register(api, token, healthy)
    sender.script[expired["endpoint"]] = ["gone"]
    api.post(f"{API}/notifications/test", headers=bearer(token))
    assert history(api, token)["items"][0]["pushStatus"] == "partial"
    devices = api.get(f"{API}/notifications/subscriptions", headers=bearer(token)).json()
    assert len(devices) == 1 and devices[0]["lastSuccessAt"]
    assert rows(isolated_db_path, "SELECT endpoint FROM push_subscriptions") == [(healthy["endpoint"],)]


def test_repeated_failures_remove_a_device_and_success_resets_the_count(make_api, sender, monkeypatch):
    api = make_api(rate_limit_settings=replace(RateLimitSettings(), enabled=False))
    monkeypatch.setattr(notifications_module, "MAX_FAILURES", 3)
    token = new_inbox(api)
    flaky = subscription()
    register(api, token, flaky)
    sender.script[flaky["endpoint"]] = ["retry", "retry", "sent", "retry", "rejected", "retry"]
    statuses = []
    for _ in range(6):
        api.post(f"{API}/notifications/test", headers=bearer(token))
        statuses.append(history(api, token)["items"][0]["pushStatus"])
        devices = api.get(f"{API}/notifications/subscriptions", headers=bearer(token)).json()
        if not devices:
            break
    assert statuses == ["failed", "failed", "sent", "failed", "failed", "failed"]
    assert devices == [], "removed after 3 failures in a row"
    api.post(f"{API}/notifications/test", headers=bearer(token))
    assert history(api, token)["items"][0]["pushStatus"] == "no_devices"
    assert len(history(api, token)["items"]) == 7, "history is kept whatever happens to delivery"


def test_history_is_kept_when_push_is_disabled(isolated_db_path, evidence_dir):
    with TestClient(create_app(db_path=isolated_db_path, evidence_dir=evidence_dir)) as api:
        token = new_inbox(api)
        register(api, token, subscription())
        api.post(f"{API}/notifications/test", headers=bearer(token))
        item = history(api, token)["items"][0]
        assert item["kind"] == "test" and item["pushStatus"] == "disabled"


def test_expired_subscriptions_are_pruned_at_startup(make_api, isolated_db_path):
    api = make_api()
    token = new_inbox(api)
    old = {**subscription(), "expirationTime": int((time.time() - 60) * 1000)}
    future = {**subscription(), "expirationTime": int((time.time() + 86400) * 1000)}
    register(api, token, old)
    register(api, token, future)
    assert len(api.get(f"{API}/notifications/subscriptions", headers=bearer(token)).json()) == 2
    restarted = make_api()
    assert len(restarted.get(f"{API}/notifications/subscriptions", headers=bearer(token)).json()) == 1
    assert rows(isolated_db_path, "SELECT endpoint FROM push_subscriptions") == [(future["endpoint"],)]


def test_notification_failure_never_breaks_the_request(api, staff, monkeypatch):
    def broken(*args, **kwargs):
        raise StoreError("database down")
    monkeypatch.setattr(ComplaintStore, "add_notifications", broken)
    receipt = api.post(f"{API}/complaints", json=payload())
    assert receipt.status_code == 201
    upload_evidence(api, receipt.json())
    monkeypatch.undo()
    assert history(api, staff["asha"])["items"] == []


def test_sender_crash_is_contained(api, staff, sender, monkeypatch):
    register(api, staff["asha"], subscription())
    monkeypatch.setattr(sender, "send", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert api.post(f"{API}/complaints", json=payload()).status_code == 201
    assert history(api, staff["asha"])["items"][0]["pushStatus"] == "pending", "stored even though delivery crashed"


# ---------- WebPushSender: real encryption and VAPID signing against a fake push service ----------

class FakePushService(requests.adapters.BaseAdapter):
    def __init__(self, status=201, error=None):
        super().__init__()
        self.status, self.error, self.requests = status, error, []

    def send(self, request, **kwargs):
        self.requests.append(request)
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code, response.request, response._content = self.status, request, b"push service says no"
        response.url = request.url
        return response

    def close(self):
        pass


def sender_with(service):
    session = requests.Session()
    session.mount("https://", service)
    return WebPushSender(SETTINGS, session=session)


def test_web_push_sender_encrypts_for_the_subscriber_and_signs_with_vapid():
    client_key, auth = ec.generate_private_key(ec.SECP256R1()), os.urandom(16)
    sub = subscription(private_key=client_key, auth=auth)
    service = FakePushService()
    message = json.dumps({"id": "nt_x", "title": "Evidence received", "url": "#/track/NCL-2026-ABCDEF"})
    assert sender_with(service).send({"endpoint": sub["endpoint"], **sub["keys"]}, message) == PushResult("sent", 201)
    [request] = service.requests
    assert request.url == sub["endpoint"]
    assert request.headers["Content-Encoding"] == "aes128gcm" and request.headers["TTL"] == str(24 * 3600)
    assert request.headers["Urgency"] == "normal"
    assert message.encode() not in request.body, "the payload is encrypted"
    assert http_ece.decrypt(request.body, private_key=client_key, auth_secret=auth, version="aes128gcm") == message.encode()

    scheme, _, params = request.headers["Authorization"].partition(" ")
    fields = dict(p.strip().split("=", 1) for p in params.split(","))
    assert scheme == "vapid" and fields["k"] == PUBLIC
    header, claims, signature = fields["t"].split(".")
    claims_json = json.loads(b64url_decode(claims))
    assert claims_json["aud"] == "https://fcm.googleapis.com" and claims_json["sub"] == "mailto:ops@example.org"
    assert time.time() < claims_json["exp"] <= time.time() + 24 * 3600 + 60
    raw = b64url_decode(signature)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(PUBLIC))
    public.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                  f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if the signature is wrong
    assert PRIVATE not in json.dumps(dict(request.headers))


@pytest.mark.parametrize("status, outcome", [(201, "sent"), (404, "gone"), (410, "gone"), (429, "retry"), (500, "retry"),
                                             (503, "retry"), (400, "rejected"), (403, "rejected"), (413, "rejected")])
def test_web_push_sender_maps_push_service_responses(status, outcome, caplog):
    sub = subscription()
    result = sender_with(FakePushService(status)).send({"endpoint": sub["endpoint"], **sub["keys"]}, "{}")
    assert result == PushResult(outcome, status)
    assert "push service says no" not in caplog.text and sub["endpoint"] not in caplog.text


def test_web_push_sender_network_errors_and_bad_keys():
    sub = subscription()
    down = sender_with(FakePushService(error=requests.ConnectionError("unreachable")))
    assert down.send({"endpoint": sub["endpoint"], **sub["keys"]}, "{}") == PushResult("retry", None)
    bad = sender_with(FakePushService())
    assert bad.send({"endpoint": sub["endpoint"], "p256dh": "AAAA", "auth": sub["keys"]["auth"]}, "{}").outcome == "rejected"


def test_default_push_service_hosts():
    assert "fcm.googleapis.com" in DEFAULT_ENDPOINT_HOSTS and "notify.windows.com" in DEFAULT_ENDPOINT_HOSTS
