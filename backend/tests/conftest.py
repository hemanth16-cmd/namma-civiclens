import os
import uuid

import pytest

from tests.dbutil import POSTGRES_MODE

# Run the suite against PostgreSQL with:
#   CIVICLENS_TEST_DATABASE=postgres CIVICLENS_TEST_POSTGRES_URL=postgresql://user:pw@127.0.0.1:55432/db pytest
# Each test then gets its own empty schema (created and dropped here). Default: SQLite files in tmp_path.
TEST_POSTGRES_URL = os.environ.get("CIVICLENS_TEST_POSTGRES_URL", "")


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: exercises SQLite-specific mechanics (files, PRAGMA, triggers)")
    config.addinivalue_line("markers", "postgres: needs a real PostgreSQL (CIVICLENS_TEST_POSTGRES_URL)")


def pytest_collection_modifyitems(config, items):
    skip_sqlite = pytest.mark.skip(reason="SQLite-specific test; the suite is running on PostgreSQL")
    skip_pg = pytest.mark.skip(reason="needs CIVICLENS_TEST_POSTGRES_URL (a real PostgreSQL)")
    for item in items:
        if POSTGRES_MODE and "sqlite_only" in item.keywords:
            item.add_marker(skip_sqlite)
        if "postgres" in item.keywords and not TEST_POSTGRES_URL:
            item.add_marker(skip_pg)


def schema_url(base_url: str, schema: str) -> str:
    """The base URL with search_path pointed at `schema`."""
    return base_url + ("&" if "?" in base_url else "?") + f"options=-csearch_path%3D{schema}"


@pytest.fixture
def postgres_schema():
    """A fresh, empty PostgreSQL schema; yields its URL and drops it afterwards."""
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:16]
    with psycopg.connect(TEST_POSTGRES_URL, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
    try:
        yield schema_url(TEST_POSTGRES_URL, schema)
    finally:
        with psycopg.connect(TEST_POSTGRES_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture(autouse=True)
def isolated_db_path(tmp_path, monkeypatch, request):
    """Point every test at its own temporary database so the development database is never touched."""
    path = tmp_path / "test.sqlite3"
    monkeypatch.setenv("CIVICLENS_DB_PATH", str(path))
    monkeypatch.setenv("CIVICLENS_EVIDENCE_DIR", str(tmp_path / "evidence"))
    # Most tests create many complaints from one test client; rate-limit tests enable limits explicitly.
    monkeypatch.setenv("CIVICLENS_RATE_LIMITS", "off")
    for name in ("CIVICLENS_TRUSTED_PROXIES", "CIVICLENS_EVIDENCE_QUOTA_BYTES", "EVIDENCE_STORAGE_BACKEND", "SUPABASE_URL",
                 "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET",
                 "DATABASE_BACKEND", "DATABASE_URL", "CIVICLENS_DB_POOL_MAX", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY",
                 "VAPID_SUBJECT", "CIVICLENS_PUSH_ENDPOINT_HOSTS", "CIVICLENS_PUSH_TTL_SECONDS",
                 # Never let a developer's real Roboflow settings reach a test (AI tests pass a fake provider).
                 "CIVICLENS_AI_PROVIDER", "ROBOFLOW_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    if not POSTGRES_MODE or "sqlite_only" in request.keywords:
        yield path
        return

    # PostgreSQL mode: a private schema per test; every pool opened during the test is closed after it.
    from app import db

    opened = []
    original_init = db.PostgresDatabase.__init__

    def tracking_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(db.PostgresDatabase, "__init__", tracking_init)
    url = request.getfixturevalue("postgres_schema")
    monkeypatch.setenv("DATABASE_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        yield path
    finally:
        for database in opened:
            database.close()


@pytest.fixture
def evidence_dir(tmp_path):
    return tmp_path / "evidence"
