import pytest
from fastapi.testclient import TestClient

from app.cors import DEFAULT_CORS_ORIGINS, resolve_cors_origins
from app.main import create_app

FRONTEND = "http://localhost:4173"


def preflight(client, origin, method="POST"):
    return client.options(
        "/api/v1/complaints",
        headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": "content-type"},
    )


def test_default_allows_only_the_local_frontend(monkeypatch):
    monkeypatch.delenv("CIVICLENS_CORS_ORIGINS", raising=False)
    assert resolve_cors_origins() == list(DEFAULT_CORS_ORIGINS) == [FRONTEND]


def test_preflight_from_the_frontend_is_allowed_without_credentials(monkeypatch):
    monkeypatch.delenv("CIVICLENS_CORS_ORIGINS", raising=False)
    response = preflight(TestClient(create_app()), FRONTEND)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert "access-control-allow-credentials" not in response.headers
    assert "POST" in response.headers["access-control-allow-methods"]


def test_simple_request_from_the_frontend_gets_the_origin_header(monkeypatch):
    monkeypatch.delenv("CIVICLENS_CORS_ORIGINS", raising=False)
    response = TestClient(create_app()).get("/api/v1/health", headers={"Origin": FRONTEND})
    assert response.headers["access-control-allow-origin"] == FRONTEND


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:5173", "http://127.0.0.1:4173", "null"])
def test_other_origins_are_not_allowed(monkeypatch, origin):
    monkeypatch.delenv("CIVICLENS_CORS_ORIGINS", raising=False)
    client = TestClient(create_app())
    assert preflight(client, origin).status_code == 400
    assert "access-control-allow-origin" not in client.get("/api/v1/health", headers={"Origin": origin}).headers


def test_preflight_rejects_unlisted_methods(monkeypatch):
    monkeypatch.delenv("CIVICLENS_CORS_ORIGINS", raising=False)
    assert preflight(TestClient(create_app()), FRONTEND, method="PUT").status_code == 400
    assert preflight(TestClient(create_app()), FRONTEND, method="PATCH").status_code == 400


def test_origins_can_be_configured_by_environment(monkeypatch):
    monkeypatch.setenv("CIVICLENS_CORS_ORIGINS", "http://127.0.0.1:4173, https://staging.example.org")
    assert resolve_cors_origins() == ["http://127.0.0.1:4173", "https://staging.example.org"]
    client = TestClient(create_app())
    assert preflight(client, "https://staging.example.org").headers["access-control-allow-origin"] == "https://staging.example.org"
    assert preflight(client, FRONTEND).status_code == 400


def test_empty_environment_value_allows_no_browser_origins(monkeypatch):
    monkeypatch.setenv("CIVICLENS_CORS_ORIGINS", "")
    assert resolve_cors_origins() == []


@pytest.mark.parametrize("bad", ["*", "localhost:4173", "ftp://localhost", "http://localhost:4173/app", "http://user@localhost:4173"])
def test_wildcards_and_malformed_origins_are_refused(bad):
    with pytest.raises(ValueError):
        create_app(cors_origins=[bad])
