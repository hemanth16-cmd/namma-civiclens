"""Manage staff accounts for the server console. Run from backend/:

    .venv/Scripts/python -m app.staff create --username asha --display-name "Asha (ward office)"
    .venv/Scripts/python -m app.staff list
    .venv/Scripts/python -m app.staff disable --username asha      # also signs the account out everywhere
    .venv/Scripts/python -m app.staff enable --username asha
    .venv/Scripts/python -m app.staff set-password --username asha # also signs the account out everywhere
    .venv/Scripts/python -m app.staff create --username ravi --display-name "Ravi" --role supervisor
    .venv/Scripts/python -m app.staff set-role --username asha --role supervisor
    .venv/Scripts/python -m app.staff department add --code roads --name "Road maintenance"
    .venv/Scripts/python -m app.staff department list
    .venv/Scripts/python -m app.staff department disable --code roads   # (or enable)
    .venv/Scripts/python -m app.staff join --username asha --department roads    # department membership
    .venv/Scripts/python -m app.staff leave --username asha --department roads
    .venv/Scripts/python -m app.staff wards seed            # zones and wards from seeds/wards_e_aasthi.json (idempotent)
    .venv/Scripts/python -m app.staff wards list
    .venv/Scripts/python -m app.staff wards import-boundaries --file wards.geojson --ward-property ward_no --source "official ward map"
    .venv/Scripts/python -m app.staff scope grant --username ravi --zone 7     # or --ward 12, or --city
    .venv/Scripts/python -m app.staff scope revoke --username ravi --zone 7
    .venv/Scripts/python -m app.staff scope list --username ravi

Roles (lifecycle.py): official (acts on complaints in their departments, their own assignments and
the unassigned triage queue), supervisor (all complaints; assigns, reopens, requests reinspection,
cancels work orders, reads the audit trail; supervisor operations within their geographic scope) and
admin (everything a supervisor can do, always city-wide). Roles, departments and geographic scope are
managed only here, never through the API. Every change is written to the audit log.

Passwords are prompted twice without echo, or read from one line of standard input with
--password-stdin (for scripts). They are never accepted as command-line arguments, never printed,
and stored only as scrypt hashes. The database follows DATABASE_BACKEND like the API: SQLite (--db,
else CIVICLENS_DB_PATH, else backend/data/civiclens.sqlite3) or PostgreSQL (DATABASE_URL). It is
created and migrated if missing.
"""

import argparse
import getpass
import re
import sys
from pathlib import Path

from .auth import USERNAME_PATTERN, hash_password, normalise_username, password_problem
from .lifecycle import ROLES, RuleViolation
from . import wards as wd
from .store import ComplaintStore, StaffExists, StoreError


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


DEPARTMENT_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def _department(store: ComplaintStore, args) -> int:
    if args.department_command == "list":
        rows = store.list_departments(include_inactive=True)
        if not rows:
            print("No departments.")
        for d in rows:
            print(f"{d['code']:<20} {d['name']:<40} {'active' if d['active'] else 'disabled'}")
        return 0
    code = args.code.strip().lower()
    if not DEPARTMENT_CODE.fullmatch(code):
        print("Department code must be 2-32 characters: a-z, 0-9 and dashes, starting with a letter or digit.", file=sys.stderr)
        return 2
    if args.department_command == "add":
        name = args.name.strip()
        if not 1 <= len(name) <= 80:
            print("Department name must be 1-80 characters.", file=sys.stderr)
            return 2
        store.create_department(code, name)
        print(f"Created department '{code}' ({name}).")
        return 0
    if not store.set_department_active(code, args.department_command == "enable"):
        print(f"No department '{code}'.", file=sys.stderr)
        return 1
    print(f"Department '{code}' {args.department_command}d.")
    return 0


def _wards(store: ComplaintStore, args) -> int:
    if args.wards_command == "seed":
        try:
            rows = wd.load_seed(args.file or wd.SEED_PATH)
        except (OSError, ValueError, KeyError) as exc:
            print(f"Seed file rejected: {exc}", file=sys.stderr)
            return 2
        report = store.seed_wards(rows)
        counts = store.ward_counts()
        print(f"Zones: {report['zonesInserted']} inserted, {report['zonesUpdated']} updated. "
              f"Wards: {report['wardsInserted']} inserted, {report['wardsUpdated']} updated, {report['unchanged']} unchanged.")
        print(f"Now {counts['operationalWards']} operational wards in {counts['operationalZones']} zones, "
              f"{counts['wards'] - counts['operationalWards']} testing; {counts['activeWards']} active; {counts['withBoundary']} with a boundary.")
        return 0
    if args.wards_command == "import-boundaries":
        try:
            boundaries = wd.parse_boundary_file(args.file, args.ward_property)
        except (OSError, ValueError) as exc:
            print(f"Boundary file rejected: {exc}", file=sys.stderr)
            return 2
        result = store.import_boundaries(boundaries, args.source.strip())
        print(f"Imported boundaries for {result['imported']} wards from '{args.source.strip()}'.")
        return 0
    rows = store.list_wards()
    if not rows:
        print("No wards. Run: python -m app.staff wards seed")
    for w in rows:
        flags = ",".join(f for f, on in (("testing", w["testing"]), ("inactive", not w["active"]), ("boundary", w["hasBoundary"])) if on) or "-"
        print(f"zone {w['zone']['number']:>2} {w['zone']['name']:<24} ward {w['number']:>3} {w['name']:<36} {flags}")
    return 0


def _scope(store: ComplaintStore, args) -> int:
    username = normalise_username(args.username)
    if args.scope_command == "list":
        rows = store.staff_scope(username)
        if not rows:
            print(f"'{username}' has no geographic scope.")
        for r in rows:
            print("city-wide" if r["type"] == "city" else f"{r['type']} {r['number']} ({r['name']})")
        return 0
    scope_type, number = ("city", None) if args.city else ("zone", args.zone) if args.zone is not None else ("ward", args.ward)
    if not store.set_staff_scope(username, scope_type, number, args.scope_command == "grant"):
        print(f"No staff account '{username}' or no {scope_type} {number}.", file=sys.stderr)
        return 1
    what = "city-wide" if scope_type == "city" else f"{scope_type} {number}"
    print(f"'{username}': {what} {'granted' if args.scope_command == 'grant' else 'revoked'}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.staff", description="Manage CivicLens staff accounts.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database (default: CIVICLENS_DB_PATH or backend/data/civiclens.sqlite3); ignored when DATABASE_BACKEND=postgres")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create a staff account")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true", help="Read the password from one line of standard input")
    create.add_argument("--role", choices=ROLES, default="official")
    sub.add_parser("list", help="List staff accounts")
    role = sub.add_parser("set-role", help="Change a staff member's role")
    role.add_argument("--username", required=True)
    role.add_argument("--role", choices=ROLES, required=True)
    dept = sub.add_parser("department", help="Manage departments")
    dept_sub = dept.add_subparsers(dest="department_command", required=True)
    add = dept_sub.add_parser("add", help="Create a department")
    add.add_argument("--code", required=True, help="Short code: a-z, 0-9 and dashes, 2-32 characters (e.g. roads)")
    add.add_argument("--name", required=True)
    dept_sub.add_parser("list", help="List departments")
    for name in ("disable", "enable"):
        dept_sub.add_parser(name, help=f"{name.capitalize()} a department").add_argument("--code", required=True)
    for name, text in (("join", "Add a staff member to a department"), ("leave", "Remove a staff member from a department")):
        member = sub.add_parser(name, help=text)
        member.add_argument("--username", required=True)
        member.add_argument("--department", required=True)
    for name in ("disable", "enable"):
        sub.add_parser(name, help=f"{name.capitalize()} a staff account").add_argument("--username", required=True)
    setpw = sub.add_parser("set-password", help="Change a staff password")
    setpw.add_argument("--username", required=True)
    setpw.add_argument("--password-stdin", action="store_true")
    wards = sub.add_parser("wards", help="Ward and zone master data")
    wards_sub = wards.add_subparsers(dest="wards_command", required=True)
    wards_sub.add_parser("seed", help="Insert or update zones and wards from the e-Aasthi seed file").add_argument("--file", type=Path, default=None)
    wards_sub.add_parser("list", help="List zones and wards")
    imp = wards_sub.add_parser("import-boundaries", help="Store official ward boundary polygons (GeoJSON FeatureCollection)")
    imp.add_argument("--file", type=Path, required=True)
    imp.add_argument("--ward-property", required=True, help="Feature property holding the ward number")
    imp.add_argument("--source", required=True, help="Where the boundary file comes from (recorded with each ward)")
    scope = sub.add_parser("scope", help="Supervisor geographic scope")
    scope_sub = scope.add_subparsers(dest="scope_command", required=True)
    for name in ("grant", "revoke"):
        g = scope_sub.add_parser(name, help=f"{name.capitalize()} city-wide, zone or ward scope")
        g.add_argument("--username", required=True)
        target = g.add_mutually_exclusive_group(required=True)
        target.add_argument("--city", action="store_true")
        target.add_argument("--zone", type=int)
        target.add_argument("--ward", type=int)
    scope_sub.add_parser("list", help="Show a staff member's scope").add_argument("--username", required=True)
    args = parser.parse_args(argv)

    store = ComplaintStore(args.db)  # DATABASE_BACKEND decides; --db is the SQLite file (ignored for postgres)
    try:
        store.initialize()
        if args.command == "list":
            rows = store.list_staff()
            if not rows:
                print("No staff accounts.")
            for r in rows:
                state = f"disabled {r['disabled_at']}" if r["disabled_at"] else "active"
                departments = ",".join(store.staff_departments(r["username"])) or "-"
                print(f"{r['username']:<24} {r['display_name']:<32} {r['role']:<10} {departments:<20} {state}")
            return 0
        if args.command == "department":
            return _department(store, args)
        if args.command == "wards":
            return _wards(store, args)
        if args.command == "scope":
            return _scope(store, args)
        username = normalise_username(args.username)
        if not USERNAME_PATTERN.fullmatch(username):
            print("Username must be 3–64 characters: a–z, 0–9, dot, dash or underscore.", file=sys.stderr)
            return 2
        if args.command == "create":
            display = args.display_name.strip()
            if not 1 <= len(display) <= 80:
                print("Display name must be 1–80 characters.", file=sys.stderr)
                return 2
            password = _read_password(args.password_stdin)
            if problem := password_problem(password):
                print(problem, file=sys.stderr)
                return 2
            store.create_staff(username, display, hash_password(password), args.role)
            print(f"Created staff account '{username}' ({args.role}).")
            return 0
        if args.command == "set-role":
            if not store.set_staff_role(username, args.role):
                print(f"No staff account '{username}'.", file=sys.stderr)
                return 1
            print(f"'{username}' is now {args.role}.")
            return 0
        if args.command in ("join", "leave"):
            if not store.set_staff_department(username, args.department.strip().lower(), args.command == "join"):
                print(f"No staff account '{username}' or department '{args.department}'.", file=sys.stderr)
                return 1
            print(f"'{username}' {'joined' if args.command == 'join' else 'left'} '{args.department}'.")
            return 0
        if args.command == "set-password":
            password = _read_password(args.password_stdin)
            if problem := password_problem(password):
                print(problem, file=sys.stderr)
                return 2
            if not store.set_staff_password(username, hash_password(password)):
                print(f"No staff account '{username}'.", file=sys.stderr)
                return 1
            print(f"Password changed for '{username}'; existing sessions were signed out.")
            return 0
        if not store.set_staff_disabled(username, args.command == "disable"):
            print(f"No staff account '{username}'.", file=sys.stderr)
            return 1
        print(f"Account '{username}' {args.command}d." + (" Existing sessions were signed out." if args.command == "disable" else ""))
        return 0
    except RuleViolation as exc:
        print(exc.message, file=sys.stderr)
        return 1
    except StaffExists:
        print(f"A staff account '{normalise_username(args.username)}' already exists.", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 3
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
