"""Database backends for ComplaintStore: SQLite (default, development) and PostgreSQL (Supabase).

store.py holds all SQL, written once with `?` placeholders. This module supplies the parts that
differ between the engines:

- connections (SQLite: one per operation; PostgreSQL: a thread-safe connection pool),
- placeholder translation (`?` → `%s` for psycopg),
- `insert()` returning the new primary key (SQLite lastrowid; PostgreSQL `RETURNING pk`),
- transactions, including an "immediate" mode for read-modify-write sections (SQLite
  BEGIN IMMEDIATE; PostgreSQL row locks taken by the caller with `FOR UPDATE`),
- schema creation and numbered migrations (SQLite PRAGMA user_version; PostgreSQL a
  civiclens_schema table, guarded by an advisory lock so concurrent starts cannot race),
- one set of exception classes (DB_ERRORS, INTEGRITY_ERRORS) for both drivers.

Configuration:
  DATABASE_BACKEND=sqlite|postgres  (default sqlite)
  DATABASE_URL=postgresql://...      (required for postgres; e.g. the Supabase connection string)
  CIVICLENS_DB_PATH=...              (SQLite file; default backend/data/civiclens.sqlite3)
  CIVICLENS_DB_POOL_MAX=10           (PostgreSQL pool size)
"""

import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

try:  # PostgreSQL support is optional at import time so SQLite-only setups need no driver.
    import psycopg
    from psycopg_pool import ConnectionPool
    _PG_ERRORS: tuple = (psycopg.Error,)
    _PG_INTEGRITY: tuple = (psycopg.IntegrityError,)
except ImportError:  # pragma: no cover - exercised only without psycopg installed
    psycopg = None
    ConnectionPool = None
    _PG_ERRORS = ()
    _PG_INTEGRITY = ()

BACKEND_ENV = "DATABASE_BACKEND"
URL_ENV = "DATABASE_URL"
DB_PATH_ENV = "CIVICLENS_DB_PATH"
POOL_MAX_ENV = "CIVICLENS_DB_POOL_MAX"
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "civiclens.sqlite3"

DB_ERRORS: tuple = (sqlite3.Error, *_PG_ERRORS)
INTEGRITY_ERRORS: tuple = (sqlite3.IntegrityError, *_PG_INTEGRITY)


class DatabaseConfigError(ValueError):
    """Database settings are invalid. The message names the variable; it never echoes the URL."""


class Row(dict):
    """A result row usable both by column name and by position, like sqlite3.Row."""

    __slots__ = ("_values",)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)

    def __iter__(self):
        # Iterate values, as sqlite3.Row does (tuple(row), unpacking). dict(row) and {**row} still
        # produce name → value mappings because they use keys().
        return iter(self._values)


# ---------- SQLite ----------

class _SQLiteConnection:
    backend = "sqlite"

    def __init__(self, path: Path):
        self.raw = sqlite3.connect(path, timeout=5)
        self.raw.row_factory = sqlite3.Row
        self.raw.execute("PRAGMA foreign_keys = ON")

    def execute(self, sql: str, params=()):
        return self.raw.execute(sql, params)

    def insert(self, sql: str, params=()) -> int:
        return self.raw.execute(sql, params).lastrowid

    @contextmanager
    def transaction(self, immediate: bool = False):
        """Commit on success, roll back on any error. `immediate` takes the write lock up front."""
        if not immediate:
            with self.raw:
                yield self
            return
        self.raw.isolation_level = None
        try:
            self.raw.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.raw.execute("COMMIT")
            except BaseException:
                if self.raw.in_transaction:
                    self.raw.execute("ROLLBACK")
                raise
        finally:
            self.raw.isolation_level = ""

    def close(self) -> None:
        self.raw.close()


class SQLiteDatabase:
    backend = "sqlite"
    for_update = ""  # SQLite locks the whole database in immediate transactions

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)

    def connect(self) -> _SQLiteConnection:
        return _SQLiteConnection(self.path)

    def apply_schema(self, schema: str, migrations: dict[int, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.raw.execute("PRAGMA journal_mode = WAL")
            conn.raw.executescript(schema)
            # A brand-new file reports 0; the base schema has just created the version-1 tables.
            version = conn.raw.execute("PRAGMA user_version").fetchone()[0] or 1
            for target in sorted(v for v in migrations if v > version):
                conn.raw.executescript(f"BEGIN;\n{migrations[target]}\nPRAGMA user_version = {target};\nCOMMIT;")
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self.connect()
        try:
            return conn.raw.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()

    def close(self) -> None:
        pass

    def describe(self) -> str:
        return f"SQLite {self.path}"


# ---------- PostgreSQL ----------

def _pg_row_factory(cursor):
    names = [d.name for d in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def _pg_sql(sql: str) -> str:
    """Translate `?` placeholders to psycopg's `%s` (store.py never puts `?` or `%` in literals)."""
    return sql.replace("%", "%%").replace("?", "%s")


class _PostgresConnection:
    backend = "postgres"

    def __init__(self, pool, raw):
        self._pool = pool
        self.raw = raw

    def execute(self, sql: str, params=()):
        return self.raw.execute(_pg_sql(sql), params)

    def insert(self, sql: str, params=()) -> int:
        return self.raw.execute(_pg_sql(sql) + " RETURNING pk", params).fetchone()[0]

    @contextmanager
    def transaction(self, immediate: bool = False):
        """Commit on success, roll back on any error. For `immediate`, the caller locks the rows it
        reads with `FOR UPDATE` (PostgresDatabase.for_update)."""
        with self.raw.transaction():
            yield self

    def close(self) -> None:
        if self.raw is not None:
            self._pool.putconn(self.raw)  # the pool rolls back anything left open
            self.raw = None


SCHEMA_TABLE = "civiclens_schema"
_SCHEMA_LOCK_ID = 7_201_026  # arbitrary constant for pg_advisory_xact_lock


class PostgresDatabase:
    backend = "postgres"
    for_update = " FOR UPDATE"

    def __init__(self, url: str, max_size: int = 10):
        if psycopg is None:
            raise DatabaseConfigError("DATABASE_BACKEND=postgres needs the psycopg packages (pip install -r requirements.txt).")
        self.url = url
        self.max_size = max_size
        self.pool = None

    def _new_pool(self):
        # Autocommit connections with explicit transaction blocks; prepare_threshold=None disables
        # server-side prepared statements, which Supabase's transaction pooler (Supavisor, port 6543)
        # and PgBouncer do not support.
        pool = ConnectionPool(self.url, min_size=1, max_size=self.max_size, open=False, timeout=10,
                              kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": _pg_row_factory})
        pool.open(wait=True, timeout=10)
        return pool

    def connect(self) -> _PostgresConnection:
        # A closed pool cannot be reopened, so a new one is created after close() (for example when
        # the same app object starts again, as in tests or server reloads).
        if self.pool is None or self.pool.closed:
            self.pool = self._new_pool()
        return _PostgresConnection(self.pool, self.pool.getconn())

    def apply_schema(self, schema: str, migrations: dict[int, str], base_version: int) -> None:
        """Create the schema at `base_version` on an empty database, then apply newer migrations.
        All of it runs in one transaction under an advisory lock."""
        conn = self.connect()
        try:
            with conn.raw.transaction():
                conn.raw.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
                conn.raw.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (version INTEGER NOT NULL)")
                row = conn.raw.execute(f"SELECT version FROM {SCHEMA_TABLE}").fetchone()
                if row is None:
                    conn.raw.execute(schema)
                    conn.raw.execute(f"INSERT INTO {SCHEMA_TABLE} (version) VALUES (%s)", (base_version,))
                    version = base_version
                else:
                    version = row[0]
                for target in sorted(v for v in migrations if v > version):
                    conn.raw.execute(migrations[target])
                    conn.raw.execute(f"UPDATE {SCHEMA_TABLE} SET version = %s", (target,))
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self.connect()
        try:
            row = conn.raw.execute(f"SELECT version FROM {SCHEMA_TABLE}").fetchone()
            return row[0] if row else 0
        finally:
            conn.close()

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()
            self.pool = None

    def describe(self) -> str:
        parts = urlsplit(self.url)
        return f"PostgreSQL {parts.hostname}:{parts.port or 5432}{parts.path}"  # never the password


# ---------- Selection ----------

def validate_postgres_url(url: str) -> str:
    parts = urlsplit(url or "")
    if parts.scheme not in ("postgresql", "postgres") or not parts.hostname:
        raise DatabaseConfigError(f"{URL_ENV} must be a postgresql://user:password@host:port/database URL "
                                  "(for Supabase: Project Settings → Database → Connection string).")
    return url


def selected_backend() -> str:
    value = os.environ.get(BACKEND_ENV, "sqlite").strip().lower() or "sqlite"
    if value not in ("sqlite", "postgres"):
        raise DatabaseConfigError(f'{BACKEND_ENV} must be "sqlite" or "postgres", not {value!r}.')
    return value


def resolve_db_path(db_path: str | os.PathLike | None = None) -> Path:
    return Path(db_path or os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def database_from_env(db_path: str | os.PathLike | None = None):
    """The configured database. `db_path` (or CIVICLENS_DB_PATH) applies to SQLite only."""
    if selected_backend() == "postgres":
        url = os.environ.get(URL_ENV, "").strip()
        if not url:
            raise DatabaseConfigError(f"{BACKEND_ENV}=postgres needs {URL_ENV}.")
        raw_max = os.environ.get(POOL_MAX_ENV, "").strip()
        if raw_max and not (raw_max.isdigit() and int(raw_max) >= 1):
            raise DatabaseConfigError(f"{POOL_MAX_ENV} must be a positive integer.")
        return PostgresDatabase(validate_postgres_url(url), max_size=int(raw_max or 10))
    return SQLiteDatabase(resolve_db_path(db_path))
