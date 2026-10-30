import pytest


@pytest.fixture(autouse=True)
def isolated_db_path(tmp_path, monkeypatch):
    """Point every test at its own temporary database so the development database is never touched."""
    path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("CIVICLENS_DB_PATH", str(path))
    monkeypatch.setenv("CIVICLENS_EVIDENCE_DIR", str(tmp_path / "evidence"))
    # Most tests create many complaints from one test client; rate-limit tests enable limits explicitly.
    monkeypatch.setenv("CIVICLENS_RATE_LIMITS", "off")
    for name in ("CIVICLENS_TRUSTED_PROXIES", "CIVICLENS_EVIDENCE_QUOTA_BYTES"):
        monkeypatch.delenv(name, raising=False)
    return path


@pytest.fixture
def evidence_dir(tmp_path):
    return tmp_path / "evidence"
