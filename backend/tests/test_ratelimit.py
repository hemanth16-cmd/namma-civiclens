import ipaddress
import threading
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ratelimit import InMemoryRateLimiter, RateLimitSettings, Rule
from tests.test_complaints import payload
from tests.test_evidence import JPEG


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_app(isolated_db_path, evidence_dir, clock):
    def make(**rules):
        settings = replace(RateLimitSettings(enabled=True), **rules)
        return create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, rate_limit_settings=settings,
                          rate_limiter=InMemoryRateLimiter(clock=clock))
    return make


def client_for(app, ip):
    return TestClient(app, client=(ip, 50000))


def assert_429(response):
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "rate_limited"
    retry = response.headers["retry-after"]
    assert retry.isdigit() and int(retry) >= 1
    return int(retry)


def test_defaults_match_the_documented_limits():
    s = RateLimitSettings()
    assert s.enabled
    assert (s.create_per_ip, s.track_per_ip) == (Rule(5, 600), Rule(60, 60))
    assert (s.token_per_ip, s.token_per_complaint) == (Rule(5, 900), Rule(10, 3600))
    assert (s.evidence_per_ip, s.evidence_per_complaint) == (Rule(30, 3600), Rule(10, 3600))
    assert s.trusted_proxies == ()


def test_settings_from_environment(monkeypatch):
    monkeypatch.setenv("CIVICLENS_RATE_LIMITS", "on")
    monkeypatch.setenv("CIVICLENS_RATE_CREATE", "2/30")
    monkeypatch.setenv("CIVICLENS_TRUSTED_PROXIES", "10.0.0.1, 192.168.0.0/16")
    s = RateLimitSettings.from_env()
    assert s.enabled and s.create_per_ip == Rule(2, 30) and s.track_per_ip == Rule(60, 60)
    assert ipaddress.ip_address("192.168.4.4") in s.trusted_proxies[1]
    monkeypatch.setenv("CIVICLENS_RATE_LIMITS", "off")
    assert not RateLimitSettings.from_env().enabled
    for bad in ("0/60", "5/0", "five/60", "5"):
        with pytest.raises(ValueError):
            Rule.parse(bad)


def test_complaint_creation_is_limited_per_ip_and_resets(make_app, clock):
    app = make_app(create_per_ip=Rule(5, 600))
    with client_for(app, "10.0.0.1") as a:
        for _ in range(5):
            assert a.post("/api/v1/complaints", json=payload()).status_code == 201
        retry = assert_429(a.post("/api/v1/complaints", json=payload()))
        assert 1 <= retry <= 600
        clock.advance(599)
        assert_429(a.post("/api/v1/complaints", json=payload()))
        clock.advance(2)
        assert a.post("/api/v1/complaints", json=payload()).status_code == 201, "window expired"


def test_retry_after_reflects_the_oldest_request(make_app, clock):
    app = make_app(create_per_ip=Rule(2, 100))
    with client_for(app, "10.0.0.1") as a:
        a.post("/api/v1/complaints", json=payload())
        clock.advance(40)
        a.post("/api/v1/complaints", json=payload())
        assert assert_429(a.post("/api/v1/complaints", json=payload())) == 60


def test_one_client_does_not_affect_another(make_app):
    app = make_app(create_per_ip=Rule(1, 600))
    with client_for(app, "10.0.0.1") as a, client_for(app, "10.0.0.2") as b:
        assert a.post("/api/v1/complaints", json=payload()).status_code == 201
        assert_429(a.post("/api/v1/complaints", json=payload()))
        assert b.post("/api/v1/complaints", json=payload()).status_code == 201


def test_rejected_requests_do_not_extend_the_window(make_app, clock):
    app = make_app(create_per_ip=Rule(1, 100))
    with client_for(app, "10.0.0.1") as a:
        a.post("/api/v1/complaints", json=payload())
        for _ in range(5):
            clock.advance(10)
            assert_429(a.post("/api/v1/complaints", json=payload()))
        clock.advance(51)
        assert a.post("/api/v1/complaints", json=payload()).status_code == 201


def test_forwarded_headers_are_ignored_without_a_trusted_proxy(make_app):
    app = make_app(create_per_ip=Rule(1, 600))
    with client_for(app, "10.0.0.1") as a:
        assert a.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 201
        assert_429(a.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "2.2.2.2"}))
        assert_429(a.post("/api/v1/complaints", json=payload(), headers={"X-Real-IP": "3.3.3.3", "Forwarded": "for=4.4.4.4"}))


def test_trusted_proxy_uses_the_right_most_untrusted_address(make_app):
    app = make_app(create_per_ip=Rule(1, 600), trusted_proxies=(ipaddress.ip_network("10.0.0.1/32"),))
    with client_for(app, "10.0.0.1") as proxy:
        assert proxy.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 201
        assert proxy.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "203.0.113.6"}).status_code == 201
        # A client cannot dodge its limit by prepending a fake address: the right-most untrusted one counts.
        assert_429(proxy.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.5"}))
    with client_for(app, "10.0.0.9") as direct:  # not a trusted proxy: its header is ignored
        assert direct.post("/api/v1/complaints", json=payload(), headers={"X-Forwarded-For": "203.0.113.5"}).status_code == 201


def test_public_tracking_is_limited(make_app):
    app = make_app(track_per_ip=Rule(3, 60))
    with client_for(app, "10.0.0.1") as a, client_for(app, "10.0.0.2") as b:
        for _ in range(3):
            assert a.get("/api/v1/complaints/NCL-2026-ZZZZZZ").status_code == 404
        assert_429(a.get("/api/v1/complaints/NCL-2026-ZZZZZZ"))
        assert b.get("/api/v1/complaints/NCL-2026-ZZZZZZ").status_code == 404


def test_token_renewal_is_limited_per_ip_and_per_complaint(make_app):
    app = make_app(token_per_ip=Rule(2, 900), token_per_complaint=Rule(3, 3600))
    with client_for(app, "10.0.0.1") as a, client_for(app, "10.0.0.2") as b, client_for(app, "10.0.0.3") as c:
        receipt = a.post("/api/v1/complaints", json=payload()).json()
        url = f"/api/v1/complaints/{receipt['id']}/upload-token"
        assert a.post(url, json={"claim": "wrong"}).status_code == 401
        assert a.post(url, json={"claim": "wrong"}).status_code == 401
        assert_429(a.post(url, json={"claim": receipt["claim"]}))  # per-IP limit, even with the right code
        assert b.post(url, json={"claim": "wrong"}).status_code == 401  # third attempt on this complaint
        assert_429(c.post(url, json={"claim": receipt["claim"]}))   # per-complaint limit, from a fresh IP


def test_token_limit_runs_before_claim_hashing(make_app, monkeypatch):
    calls = []
    import app.complaints as complaints
    real = complaints.verify_claim_code
    monkeypatch.setattr(complaints, "verify_claim_code", lambda *a: calls.append(1) or real(*a))
    app = make_app(token_per_ip=Rule(1, 900))
    with client_for(app, "10.0.0.1") as a:
        receipt = a.post("/api/v1/complaints", json=payload()).json()
        url = f"/api/v1/complaints/{receipt['id']}/upload-token"
        a.post(url, json={"claim": "wrong"})
        assert_429(a.post(url, json={"claim": "wrong"}))
        assert_429(a.post("/api/v1/complaints/NCL-2026-ZZZZZZ/upload-token", json={"claim": "x"}))
    assert len(calls) == 1, "scrypt verification ran only for the allowed request"


def upload(client, receipt, token=None):
    return client.post(f"/api/v1/complaints/{receipt['id']}/evidence", files={"file": ("e.jpg", JPEG, "image/jpeg")},
                       headers={"Authorization": f"Bearer {token or receipt['uploadToken']}"})


def test_evidence_uploads_are_limited_per_complaint_after_authorisation(make_app):
    app = make_app(evidence_per_complaint=Rule(3, 3600))
    with client_for(app, "10.0.0.1") as owner, client_for(app, "10.0.0.2") as stranger:
        receipt = owner.post("/api/v1/complaints", json=payload()).json()
        for _ in range(5):  # invalid tokens are refused but do not use up the complaint's allowance
            assert upload(stranger, receipt, token="X" * 43).status_code == 401
        assert upload(owner, receipt).status_code == 201
        assert upload(owner, receipt).status_code == 200
        assert upload(owner, receipt).status_code == 200
        assert_429(upload(owner, receipt))


def test_evidence_per_ip_limit_runs_before_token_check_and_body_reading(make_app, monkeypatch):
    app = make_app(evidence_per_ip=Rule(2, 3600))
    checked, started = [], []
    with client_for(app, "10.0.0.1") as a:
        receipt = a.post("/api/v1/complaints", json=payload()).json()
        real_check = app.state.store.check_upload_token
        monkeypatch.setattr(app.state.store, "check_upload_token", lambda *x: checked.append(1) or real_check(*x))
        real_begin = app.state.evidence_storage.begin
        monkeypatch.setattr(app.state.evidence_storage, "begin", lambda: started.append(1) or real_begin())
        assert upload(a, receipt).status_code == 201
        assert upload(a, receipt).status_code == 200
        assert_429(upload(a, receipt))
    assert (len(checked), len(started)) == (2, 2), "the limited request did no token lookup and stored nothing"


def test_limits_can_be_disabled(isolated_db_path, evidence_dir):
    app = create_app(db_path=isolated_db_path, evidence_dir=evidence_dir, rate_limit_settings=RateLimitSettings(enabled=False, create_per_ip=Rule(1, 600)))
    with client_for(app, "10.0.0.1") as a:
        assert all(a.post("/api/v1/complaints", json=payload()).status_code == 201 for _ in range(3))


def test_limiter_is_thread_safe(clock):
    limiter = InMemoryRateLimiter(clock=clock)
    results = []
    rule = Rule(50, 60)

    def worker():
        for _ in range(20):
            results.append(limiter.hit("k", rule))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for r in results if r == 0) == 50


def test_limiter_memory_is_bounded(clock):
    limiter = InMemoryRateLimiter(clock=clock, max_keys=100)
    for i in range(1000):
        limiter.hit(f"ip-{i}", Rule(1, 60))
    assert len(limiter) == 100
    assert limiter.hit("ip-999", Rule(1, 60)) > 0, "recent keys are kept"
    assert limiter.hit("ip-0", Rule(1, 60)) == 0, "the oldest keys were evicted"
