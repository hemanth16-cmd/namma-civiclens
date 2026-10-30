"""Copy a CivicLens SQLite database into PostgreSQL (for example Supabase). Dry run by default.

    cd backend
    .venv/Scripts/python -m app.db_migrate --sqlite data/civiclens.sqlite3                 # plan only
    .venv/Scripts/python -m app.db_migrate --sqlite data/civiclens.sqlite3 --execute       # copy
    .venv/Scripts/python -m app.db_migrate --sqlite data/civiclens.sqlite3 --json          # machine-readable

The target is DATABASE_URL (or --postgres-url). Safety rules:
- The source is never modified. It is copied with SQLite's online backup API from a read-only
  connection into a temporary file (a consistent snapshot, including any WAL contents); an older
  schema is upgraded on that copy only. The copy is deleted afterwards.
- Without --execute nothing is written to PostgreSQL either (not even the schema).
- With --execute the schema is created if missing, then every row is inserted with its original
  primary key inside ONE transaction, so complaint IDs and all relationships are preserved. Identity
  sequences are advanced past the imported keys. Any error rolls the whole import back.
- Re-running is safe: rows already present with identical values are skipped. A row whose key exists
  with different values, or that would break a unique constraint, is a conflict: nothing is written
  and the conflicting table/key is reported.
- After writing (and in dry runs, for rows already present) every source row is compared column by
  column with PostgreSQL, counts are checked, and foreign keys are verified.
- The report contains table names, counts and primary keys only: never row contents, hashes or the
  database password.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from .db import DB_ERRORS, DatabaseConfigError, PostgresDatabase, SQLiteDatabase, URL_ENV, validate_postgres_url
from .store import SCHEMA_VERSION, TABLES, ComplaintStore, StoreError

# Foreign keys to verify after import: (child table, child column, parent table).
FOREIGN_KEYS = (
    ("complaint_timeline", "complaint_pk", "complaints"),
    ("verification_log", "complaint_pk", "complaints"),
    ("evidence", "complaint_pk", "complaints"),
    ("idempotency_keys", "complaint_pk", "complaints"),
    ("staff_sessions", "staff_pk", "staff_accounts"),
    ("evidence_access_log", "evidence_pk", "evidence"),
    ("evidence_access_log", "staff_pk", "staff_accounts"),
    ("notification_recipients", "staff_pk", "staff_accounts"),
    ("notification_follows", "recipient_pk", "notification_recipients"),
    ("notification_follows", "complaint_pk", "complaints"),
    ("push_subscriptions", "recipient_pk", "notification_recipients"),
    ("notifications", "recipient_pk", "notification_recipients"),
    ("notifications", "complaint_pk", "complaints"),
    ("ai_inferences", "complaint_pk", "complaints"),
    ("ai_inferences", "evidence_pk", "evidence"),
    ("repair_assessments", "complaint_pk", "complaints"),
    ("repair_assessments", "original_evidence_pk", "evidence"),
    ("repair_assessments", "repair_evidence_pk", "evidence"),
    ("repair_assessments", "work_order_pk", "work_orders"),
    ("repair_assessment_inferences", "assessment_pk", "repair_assessments"),
    ("repair_assessment_inferences", "inference_pk", "ai_inferences"),
)
# Columns that are JSON text in SQLite and JSONB in PostgreSQL. JSONB does not keep key order or
# whitespace, so these are compared as parsed values.
JSON_COLUMNS = {("ai_inferences", "predictions"), ("ai_inferences", "confidence_meta"), ("repair_assessments", "details")}
_IMPORT_LOCK_ID = 7_201_027


class MigrationError(Exception):
    """The migration cannot proceed. The message is safe to print."""


def _snapshot(source: Path, workdir: Path) -> Path:
    """A consistent, upgraded copy of the source database. The source itself is opened read-only."""
    if not source.is_file():
        raise MigrationError(f"SQLite database not found: {source}")
    copy = workdir / "snapshot.sqlite3"
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        version = src.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise MigrationError(f"The source database is schema version {version}; this code supports {SCHEMA_VERSION}. Update the code first.")
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    ComplaintStore(database=SQLiteDatabase(copy)).initialize()  # upgrade the copy only
    return copy


def _columns_sqlite(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _columns_pg(conn, table: str) -> list[str]:
    rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = ? "
                        "ORDER BY ordinal_position", (table,)).fetchall()
    return [r[0] for r in rows]


def _pg_has_schema(conn) -> bool:
    return conn.execute("SELECT to_regclass('civiclens_schema') IS NOT NULL").fetchone()[0]


def _norm(value, json_column: bool = False):
    if json_column and value is not None:
        parsed = json.loads(value) if isinstance(value, str) else value
        return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return float(value) if isinstance(value, float) else value


def _norm_row(table: str, cols: list[str], row) -> tuple:
    return tuple(_norm(v, (table, c) in JSON_COLUMNS) for c, v in zip(cols, row))


def migrate(sqlite_path: str | os.PathLike, postgres: PostgresDatabase, execute: bool = False) -> dict:
    """Plan (and with execute=True, perform) the import. Returns a report; raises MigrationError."""
    report = {"mode": "execute" if execute else "dry-run", "source": str(sqlite_path), "target": postgres.describe(),
              "schemaVersion": SCHEMA_VERSION, "tables": {}, "conflicts": [], "validation": {}, "ok": False}
    workdir = Path(tempfile.mkdtemp(prefix="civiclens-migrate-"))
    try:
        snapshot = _snapshot(Path(sqlite_path), workdir)
        src = sqlite3.connect(snapshot)
        try:
            source_rows = {}
            for table in TABLES:
                cols = _columns_sqlite(src, table)
                source_rows[table] = (cols, src.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY pk").fetchall())
        finally:
            src.close()

        if execute:
            ComplaintStore(database=postgres).initialize()
        conn = postgres.connect()
        try:
            schema_exists = _pg_has_schema(conn)
            report["targetSchemaExists"] = schema_exists
            if schema_exists:
                version = postgres.schema_version()
                if version != SCHEMA_VERSION:
                    raise MigrationError(f"PostgreSQL schema is version {version}; expected {SCHEMA_VERSION}.")

            plan = {}
            for table in TABLES:
                cols, rows = source_rows[table]
                existing = {}
                if schema_exists:
                    pg_cols = _columns_pg(conn, table)
                    if set(pg_cols) != set(cols):
                        raise MigrationError(f"Column mismatch in table {table}: SQLite and PostgreSQL schemas differ.")
                    pk_at = cols.index("pk")
                    for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall():
                        existing[r[pk_at]] = _norm_row(table, cols, r)
                pk_index = cols.index("pk")
                new, same, conflicts = [], 0, []
                for row in rows:
                    values = _norm_row(table, cols, row)
                    current = existing.get(row[pk_index])
                    if current is None:
                        new.append(row)
                    elif current == values:
                        same += 1
                    else:
                        conflicts.append(row[pk_index])
                plan[table] = (cols, new)
                report["tables"][table] = {"source": len(rows), "toInsert": len(new), "alreadyPresent": same,
                                           "conflicts": len(conflicts), "targetBefore": len(existing)}
                report["conflicts"] += [{"table": table, "pk": pk} for pk in conflicts]
            if report["conflicts"]:
                report["stopped"] = "Some rows already exist in PostgreSQL with different values; nothing was written."
                return report

            if execute:
                try:
                    with conn.transaction():
                        conn.execute("SELECT pg_advisory_xact_lock(?)", (_IMPORT_LOCK_ID,))
                        for table in TABLES:
                            cols, new = plan[table]
                            sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
                            for row in new:
                                conn.execute(sql, row)
                            conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'pk'), "
                                         f"GREATEST((SELECT COALESCE(MAX(pk), 0) FROM {table}), 1), "
                                         f"(SELECT COUNT(*) > 0 FROM {table}))")
                except DB_ERRORS as exc:
                    raise MigrationError(f"The import was rolled back: {type(exc).__name__} (a unique or foreign-key "
                                         "constraint failed). PostgreSQL is unchanged.") from exc
                for table in TABLES:
                    report["tables"][table]["inserted"] = len(plan[table][1])

            if execute or schema_exists:
                report["validation"] = _validate(conn, source_rows)
                report["ok"] = report["validation"]["rowsMatch"] and report["validation"]["foreignKeysValid"] \
                    and (execute or all(t["toInsert"] == 0 for t in report["tables"].values()))
            else:
                report["validation"] = {"note": "Target schema does not exist yet; it will be created by --execute."}
                report["ok"] = False
        finally:
            conn.close()
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _validate(conn, source_rows) -> dict:
    """Every source row must exist in PostgreSQL with identical values; foreign keys must resolve."""
    mismatched, counts = [], {}
    for table, (cols, rows) in source_rows.items():
        target = {}
        for r in conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall():
            target[r[cols.index("pk")]] = _norm_row(table, cols, r)
        missing = [row[cols.index("pk")] for row in rows if target.get(row[cols.index("pk")]) != _norm_row(table, cols, row)]
        mismatched += [{"table": table, "pk": pk} for pk in missing]
        counts[table] = {"source": len(rows), "target": len(target)}
    orphans = {}
    for child, column, parent in FOREIGN_KEYS:
        # Nullable references (a citizen recipient has no staff_pk) are not orphans.
        n = conn.execute(f"SELECT COUNT(*) FROM {child} c LEFT JOIN {parent} p ON p.pk = c.{column} "
                         f"WHERE c.{column} IS NOT NULL AND p.pk IS NULL").fetchone()[0]
        if n:
            orphans[f"{child}.{column}"] = n
    return {"rowsMatch": not mismatched, "mismatched": mismatched[:50], "counts": counts,
            "foreignKeysValid": not orphans, "orphans": orphans}


def _print_human(report: dict) -> None:
    print(f"SQLite -> PostgreSQL migration ({report['mode']})")
    print(f"  source: {report['source']}")
    print(f"  target: {report['target']}  (schema {'exists' if report.get('targetSchemaExists') else 'not created yet'})")
    print(f"  {'table':<22}{'source':>8}{'insert':>8}{'present':>9}{'conflict':>10}")
    for table, t in report["tables"].items():
        print(f"  {table:<22}{t['source']:>8}{t['toInsert']:>8}{t['alreadyPresent']:>9}{t['conflicts']:>10}")
    v = report.get("validation", {})
    if "rowsMatch" in v:
        print(f"  validation: rows match = {v['rowsMatch']}, foreign keys valid = {v['foreignKeysValid']}")
    elif v.get("note"):
        print(f"  {v['note']}")
    for c in report["conflicts"][:20]:
        print(f"  conflict: {c['table']} pk={c['pk']}")
    if report.get("stopped"):
        print(f"  STOPPED: {report['stopped']}")
    elif report["mode"] == "dry-run":
        print("  Dry run: nothing was written. Add --execute to copy the rows.")
    print("  RESULT: " + ("OK" if report["ok"] else "NOT COMPLETE"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db_migrate", description="Copy a CivicLens SQLite database into PostgreSQL.")
    parser.add_argument("--sqlite", type=Path, required=True, help="Source SQLite file (never modified)")
    parser.add_argument("--postgres-url", default=None, help=f"Target PostgreSQL URL (default: {URL_ENV}); prefer the environment variable")
    parser.add_argument("--execute", action="store_true", help="Actually write to PostgreSQL (default: dry run)")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    database = None
    try:
        url = args.postgres_url or os.environ.get(URL_ENV, "").strip()
        if not url:
            raise DatabaseConfigError(f"Set {URL_ENV} (or --postgres-url) to the target PostgreSQL connection string.")
        database = PostgresDatabase(validate_postgres_url(url))
        report = migrate(args.sqlite, database, execute=args.execute)
    except MigrationError as exc:
        print(f"Migration stopped: {exc}", file=sys.stderr)
        return 1
    except (DatabaseConfigError, StoreError, *DB_ERRORS) as exc:
        print(f"Migration failed: {type(exc).__name__}: {exc if isinstance(exc, (DatabaseConfigError, StoreError)) else 'database error'}", file=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.close()
    print(json.dumps(report, indent=2)) if args.json else _print_human(report)
    if report.get("stopped"):
        return 1
    return 0 if report["ok"] or report["mode"] == "dry-run" else 1


if __name__ == "__main__":
    sys.exit(main())
