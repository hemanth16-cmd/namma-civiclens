"""Evidence storage audit: find files with no database record, and records with no file.

Read-only by default. Run from backend/:

    .venv/Scripts/python -m app.evidence_audit                       # report only
    .venv/Scripts/python -m app.evidence_audit --json                # machine-readable report
    .venv/Scripts/python -m app.evidence_audit --delete-orphans --min-age-minutes 60

Paths default to the server's (CIVICLENS_DB_PATH, CIVICLENS_EVIDENCE_DIR, else backend/data/...);
override with --db and --evidence-dir.

Safety rules:
- The database is opened read-only (SQLite mode=ro). This tool never changes database rows.
- Only regular files under <evidence-dir>/objects whose names are valid storage keys can be
  deleted; anything else is reported as "unexpected" and left alone. Symlinks are skipped.
- Deletion requires --delete-orphans. Files younger than --min-age-minutes (default 60) are kept,
  so an upload that is finishing is not removed.
- Immediately before each deletion the database is queried again for that key.
- With --delete-stale-temp, abandoned tmp/*.part files older than the same age are removed.
- Missing files referenced by records are only reported, never "fixed".
- The report contains storage keys and sizes only: no claim codes, tokens or complaint details.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from .evidence_storage import KEY_PATTERN, LocalEvidenceStorage
from .db import DB_ERRORS, DatabaseConfigError, PostgresDatabase, database_from_env, selected_backend
from .main import DEFAULT_EVIDENCE_DIR, EVIDENCE_DIR_ENV, PUBLIC_DIR
from .store import resolve_db_path


def _pg_read(db: PostgresDatabase, sql: str, params=()) -> list:
    """One query in a READ ONLY transaction: the audit never changes PostgreSQL either."""
    conn = db.connect()
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"Database not found: {db_path}")
    return sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _referenced_keys(db_path) -> dict[str, int]:
    if isinstance(db_path, PostgresDatabase):
        return {r[0]: r[1] for r in _pg_read(db_path, "SELECT storage_key, byte_size FROM evidence WHERE storage_backend = 'local'")}
    with closing(_connect_ro(db_path)) as conn:
        has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'evidence'").fetchone()
        if not has_table:
            return {}
        columns = {row[1] for row in conn.execute("PRAGMA table_info(evidence)")}
        # Only rows stored on local disk belong in this folder; Supabase rows (schema 5+) are not in this folder.
        where = " WHERE storage_backend = 'local'" if "storage_backend" in columns else ""
        return {key: size for key, size in conn.execute(f"SELECT storage_key, byte_size FROM evidence{where}")}


def _still_unreferenced(db_path, key: str) -> bool:
    if isinstance(db_path, PostgresDatabase):
        return not _pg_read(db_path, "SELECT 1 FROM evidence WHERE storage_key = ?", (key,))
    with closing(_connect_ro(db_path)) as conn:
        return conn.execute("SELECT 1 FROM evidence WHERE storage_key = ?", (key,)).fetchone() is None


def audit(db_path: Path, evidence_dir: Path, *, delete_orphans: bool = False, delete_stale_temp: bool = False,
          min_age_seconds: float = 3600, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    storage = LocalEvidenceStorage(evidence_dir, forbidden_roots=[PUBLIC_DIR])
    referenced = _referenced_keys(db_path)

    on_disk, unexpected = {}, []
    for name, path, size in storage.iter_objects():
        if KEY_PATTERN.fullmatch(name) and path == storage.objects_dir / name[:2] / name:
            on_disk[name] = (path, size)
        else:
            unexpected.append({"path": str(path.relative_to(storage.root)), "bytes": size})

    orphans = sorted(k for k in on_disk if k not in referenced)
    missing = sorted(k for k in referenced if k not in on_disk)
    mismatched = sorted(k for k in referenced if k in on_disk and on_disk[k][1] != referenced[k])
    temps = [{"name": p.name, "bytes": size, "ageSeconds": round(now - mtime)} for p, size, mtime in storage.temp_files()]

    deleted, kept_recent, deleted_temp = [], [], []
    if delete_orphans:
        for key in orphans:
            path, _ = on_disk[key]
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < min_age_seconds:
                kept_recent.append(key)
                continue
            if not _still_unreferenced(db_path, key):  # an upload may have just recorded it
                continue
            target = storage.path_for(key)  # validates the key and keeps the path inside objects/
            if target.resolve() != path.resolve() or target.is_symlink():
                continue
            target.unlink(missing_ok=True)
            deleted.append(key)
    if delete_stale_temp:
        for path, _size, mtime in list(storage.temp_files()):
            if now - mtime >= min_age_seconds and path.parent.resolve() == (storage.root / "tmp").resolve():
                path.unlink(missing_ok=True)
                deleted_temp.append(path.name)

    return {
        "database": db_path.describe() if isinstance(db_path, PostgresDatabase) else str(db_path),
        "evidenceDir": str(storage.root),
        "mode": "delete" if (delete_orphans or delete_stale_temp) else "dry-run",
        "recordedFiles": len(referenced),
        "recordedBytes": sum(referenced.values()),
        "filesOnDisk": len(on_disk),
        "bytesOnDisk": sum(size for _, size in on_disk.values()),
        "orphans": [{"key": k, "bytes": on_disk[k][1]} for k in orphans],
        "orphanBytes": sum(on_disk[k][1] for k in orphans),
        "missingFiles": missing,
        "sizeMismatches": mismatched,
        "unexpectedFiles": unexpected,
        "tempFiles": temps,
        "deletedOrphans": deleted,
        "keptRecentOrphans": kept_recent,
        "deletedTempFiles": deleted_temp,
    }


def _print_human(report: dict) -> None:
    mb = lambda n: f"{n / 1024 / 1024:.2f} MB"
    print(f"Evidence audit ({report['mode']})")
    print(f"  database:      {report['database']}")
    print(f"  evidence dir:  {report['evidenceDir']}")
    print(f"  recorded:      {report['recordedFiles']} files, {mb(report['recordedBytes'])}")
    print(f"  on disk:       {report['filesOnDisk']} files, {mb(report['bytesOnDisk'])}")
    print(f"  orphans:       {len(report['orphans'])} files, {mb(report['orphanBytes'])} (no database record)")
    print(f"  missing files: {len(report['missingFiles'])} (record exists, file does not)")
    print(f"  size mismatch: {len(report['sizeMismatches'])}")
    print(f"  unexpected:    {len(report['unexpectedFiles'])} (not named like a storage key; never deleted)")
    print(f"  temp uploads:  {len(report['tempFiles'])}")
    for label, key in (("orphan", "orphans"),):
        for item in report[key]:
            print(f"    {label}: {item['key']} ({item['bytes']} bytes)")
    for key in report["missingFiles"]:
        print(f"    missing: {key}")
    if report["mode"] == "delete":
        print(f"  deleted orphans: {len(report['deletedOrphans'])}; kept (too recent): {len(report['keptRecentOrphans'])}; "
              f"deleted temp files: {len(report['deletedTempFiles'])}")
    else:
        print("  No changes were made. Use --delete-orphans (and --min-age-minutes) to remove orphans.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evidence_audit", description="Audit private evidence storage against the database.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database (default: CIVICLENS_DB_PATH or backend/data/civiclens.sqlite3); "
                        "with DATABASE_BACKEND=postgres, DATABASE_URL is read instead (read-only transactions)")
    parser.add_argument("--evidence-dir", type=Path, default=None, help="Evidence folder (default: CIVICLENS_EVIDENCE_DIR or backend/data/evidence)")
    parser.add_argument("--delete-orphans", action="store_true", help="DESTRUCTIVE: delete orphan files older than --min-age-minutes")
    parser.add_argument("--delete-stale-temp", action="store_true", help="DESTRUCTIVE: delete abandoned tmp/*.part files older than --min-age-minutes")
    parser.add_argument("--min-age-minutes", type=float, default=60.0, help="Never delete files younger than this (default 60)")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    args = parser.parse_args(argv)
    if args.min_age_minutes < 0:
        parser.error("--min-age-minutes must not be negative")
    evidence_dir = Path(args.evidence_dir or os.environ.get(EVIDENCE_DIR_ENV) or DEFAULT_EVIDENCE_DIR)
    source = None
    try:
        source = database_from_env() if selected_backend() == "postgres" else resolve_db_path(args.db)
        report = audit(source, evidence_dir, delete_orphans=args.delete_orphans, delete_stale_temp=args.delete_stale_temp,
                       min_age_seconds=args.min_age_minutes * 60)
    except (FileNotFoundError, DatabaseConfigError, *DB_ERRORS) as exc:
        print(f"Audit failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if isinstance(source, PostgresDatabase):
            source.close()
    print(json.dumps(report, indent=2)) if args.json else _print_human(report)
    return 1 if (report["missingFiles"] or report["sizeMismatches"]) else 0


if __name__ == "__main__":
    sys.exit(main())
