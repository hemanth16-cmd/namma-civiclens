"""Direct database access for tests, on whichever backend the test run uses.

`connect(db_path)` returns a sqlite3 connection in the default SQLite mode, and an equivalent
PostgreSQL connection (to the test's private schema) when the suite runs with
CIVICLENS_TEST_DATABASE=postgres. Both accept `?` placeholders, return tuples by default, and switch
to name-addressable rows when `row_factory` is assigned (as tests do with sqlite3.Row).
"""

import os
import sqlite3

POSTGRES_MODE = os.environ.get("CIVICLENS_TEST_DATABASE", "").lower() == "postgres"


class _PGTestConnection:
    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import tuple_row

        from app.db import _pg_row_factory, _pg_sql

        self._translate = _pg_sql
        self._dict_rows = _pg_row_factory
        self._tuple_rows = tuple_row
        self._conn = psycopg.connect(url, prepare_threshold=None)

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, _value):
        self._conn.row_factory = self._dict_rows

    def execute(self, sql: str, params=()):
        return self._conn.execute(self._translate(sql), params)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def connect(db_path):
    if POSTGRES_MODE:
        return _PGTestConnection(os.environ["DATABASE_URL"])
    return sqlite3.connect(db_path)
