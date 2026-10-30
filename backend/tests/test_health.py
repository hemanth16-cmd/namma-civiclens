from fastapi.testclient import TestClient
from pydantic import BaseModel

from app import API_VERSION, APP_VERSION
from app.main import create_app


def client(**kwargs) -> TestClient:
    return TestClient(create_app(), **kwargs)


def assert_error(response, status: int, code: str) -> dict:
    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    return body["error"]


def test_health_reports_ok_and_versions():
    response = client().get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "namma-civiclens-api",
        "api_version": API_VERSION,
        "version": APP_VERSION,
    }


def test_health_is_versioned_under_api_v1_only():
    assert client().get("/health").status_code == 404
    assert client().get("/api/health").status_code == 404


def test_unknown_route_returns_error_envelope():
    assert_error(client().get("/api/v1/does-not-exist"), 404, "not_found")


def test_wrong_method_returns_error_envelope():
    error = assert_error(client().post("/api/v1/health"), 405, "method_not_allowed")
    assert "details" not in error


def test_validation_errors_list_fields():
    app = create_app()

    class Payload(BaseModel):
        category: str
        latitude: float

    @app.post("/api/v1/_echo")
    def echo(payload: Payload):
        return payload

    error = assert_error(TestClient(app).post("/api/v1/_echo", json={"latitude": "north"}), 422, "validation_error")
    fields = {d["field"] for d in error["details"]}
    assert {"body.category", "body.latitude"} <= fields


def test_unexpected_errors_do_not_leak_internals():
    app = create_app()

    @app.get("/api/v1/_boom")
    def boom():
        raise RuntimeError("secret internal detail")

    response = TestClient(app, raise_server_exceptions=False).get("/api/v1/_boom")
    error = assert_error(response, 500, "internal_error")
    assert "secret" not in response.text


def test_openapi_document_is_served():
    response = client().get("/api/v1/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/health" in response.json()["paths"]
