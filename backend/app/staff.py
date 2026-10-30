"""Manage staff accounts for the server console. Run from backend/:

    .venv/Scripts/python -m app.staff create --username asha --display-name "Asha (ward office)"
    .venv/Scripts/python -m app.staff list
    .venv/Scripts/python -m app.staff disable --username asha      # also signs the account out everywhere
    .venv/Scripts/python -m app.staff enable --username asha
    .venv/Scripts/python -m app.staff set-password --username asha # also signs the account out everywhere

Passwords are prompted twice without echo, or read from one line of standard input with
--password-stdin (for scripts). They are never accepted as command-line arguments, never printed,
and stored only as scrypt hashes. The database comes from --db, else CIVICLENS_DB_PATH, else
backend/data/civiclens.sqlite3 (created and migrated if missing).
"""

import argparse
import getpass
import sys
from pathlib import Path

from .auth import USERNAME_PATTERN, hash_password, normalise_username, password_problem
from .store import ComplaintStore, StaffExists, StoreError, resolve_db_path


def _read_password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.staff", description="Manage CivicLens staff accounts.")
    parser.add_argument("--db", type=Path, default=None, help="SQLite database (default: CIVICLENS_DB_PATH or backend/data/civiclens.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="Create a staff account")
    create.add_argument("--username", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument("--password-stdin", action="store_true", help="Read the password from one line of standard input")
    sub.add_parser("list", help="List staff accounts")
    for name in ("disable", "enable"):
        sub.add_parser(name, help=f"{name.capitalize()} a staff account").add_argument("--username", required=True)
    setpw = sub.add_parser("set-password", help="Change a staff password")
    setpw.add_argument("--username", required=True)
    setpw.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)

    store = ComplaintStore(resolve_db_path(args.db))
    try:
        store.initialize()
        if args.command == "list":
            rows = store.list_staff()
            if not rows:
                print("No staff accounts.")
            for r in rows:
                state = f"disabled {r['disabled_at']}" if r["disabled_at"] else "active"
                print(f"{r['username']:<24} {r['display_name']:<32} {r['role']:<9} {state}")
            return 0
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
            store.create_staff(username, display, hash_password(password))
            print(f"Created staff account '{username}'.")
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
    except StaffExists:
        print(f"A staff account '{normalise_username(args.username)}' already exists.", file=sys.stderr)
        return 1
    except StoreError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
