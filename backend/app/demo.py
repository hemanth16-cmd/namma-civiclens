"""DEMONSTRATION ACCOUNTS ONLY. Creates or updates two accounts for a local or isolated test
environment, so the resident and admin journeys can be shown without typing new details:

    resident   resident@civiclens.demo   CivicDemo2026!   (resident account, marked demo)
    admin      demo_admin                CivicDemo2026!   (staff account, role "admin")

Run from backend/:

    python -m app.demo seed --confirm-demo                     # SQLite (--db, else CIVICLENS_DB_PATH)
    python -m app.demo seed --confirm-demo --allow-postgres    # an ISOLATED PostgreSQL test database only
    python -m app.demo remove --confirm-demo                   # disable both demo accounts again

The password is public and documented, so these accounts are NOT production-safe. Safeguards:

- nothing happens without --confirm-demo, and the target database is printed first;
- PostgreSQL (e.g. Supabase) needs --allow-postgres as well;
- it refuses outright when CIVICLENS_ENV is "production" or "prod";
- the API never creates these accounts by itself, and the frontend never contains the credentials.

It is idempotent: running it again leaves existing accounts in place, re-enables them, resets the
password only if it differs (which signs out their sessions) and keeps the admin role. The admin is
created through the same store methods as `python -m app.staff`, so roles stay server-side.
"""

import argparse
import os
import sys

from .auth import hash_password as hash_staff_password, normalise_username, verify_password
from .resident_auth import hash_password as hash_resident_password
from .store import ComplaintStore, ResidentExists, StaffExists, StoreError

DEMO_PASSWORD = "CivicDemo2026!"  # DEMO ONLY: public, documented, never for real accounts
DEMO_RESIDENT_EMAIL = "resident@civiclens.demo"
DEMO_RESIDENT_NAME = "Demo resident"
DEMO_ADMIN_USERNAME = "demo_admin"
DEMO_ADMIN_NAME = "Demo administrator"
DEMO_ADMIN_ROLE = "admin"
PRODUCTION_VALUES = {"production", "prod"}


def seed(store: ComplaintStore, password: str = DEMO_PASSWORD) -> dict:
    """Create or update both demo accounts. Returns what was done (no secrets)."""
    store.initialize()
    report = {}

    resident = store.get_resident(DEMO_RESIDENT_EMAIL)
    if resident is None:
        try:
            store.create_resident(DEMO_RESIDENT_EMAIL, DEMO_RESIDENT_NAME, hash_resident_password(password), is_demo=True)
            report["resident"] = "created"
        except ResidentExists:  # created concurrently
            resident = store.get_resident(DEMO_RESIDENT_EMAIL)
    if resident is not None:
        same = verify_password(password, resident["password_hash"])
        store.update_resident(DEMO_RESIDENT_EMAIL, password_hash=None if same else hash_resident_password(password),
                              disabled=False if resident["disabled_at"] else None, is_demo=True)
        report["resident"] = "unchanged" if same and not resident["disabled_at"] else "updated"

    admin = store.get_staff(DEMO_ADMIN_USERNAME)
    if admin is None:
        try:
            store.create_staff(DEMO_ADMIN_USERNAME, DEMO_ADMIN_NAME, hash_staff_password(password), role=DEMO_ADMIN_ROLE)
            report["admin"] = "created"
        except StaffExists:
            admin = store.get_staff(DEMO_ADMIN_USERNAME)
    if admin is not None:
        changed = False
        if not verify_password(password, admin["password_hash"]):
            store.set_staff_password(DEMO_ADMIN_USERNAME, hash_staff_password(password))
            changed = True
        if admin["role"] != DEMO_ADMIN_ROLE:
            store.set_staff_role(DEMO_ADMIN_USERNAME, DEMO_ADMIN_ROLE)
            changed = True
        if admin["disabled_at"] is not None:
            store.set_staff_disabled(DEMO_ADMIN_USERNAME, False)
            changed = True
        report["admin"] = "updated" if changed else "unchanged"
    return report


def remove(store: ComplaintStore) -> dict:
    """Disable both demo accounts (rows are kept: complaints may reference them). Signs them out."""
    store.initialize()
    return {"resident": "disabled" if store.update_resident(DEMO_RESIDENT_EMAIL, disabled=True) else "absent",
            "admin": "disabled" if store.set_staff_disabled(DEMO_ADMIN_USERNAME, True) else "absent"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo", description="Demonstration accounts (NOT for production).")
    parser.add_argument("command", choices=["seed", "remove"])
    parser.add_argument("--db", help="SQLite database file (ignored when DATABASE_BACKEND=postgres)")
    parser.add_argument("--confirm-demo", action="store_true", help="required: confirms this is a demo or test database")
    parser.add_argument("--allow-postgres", action="store_true", help="also required for an (isolated) PostgreSQL database")
    args = parser.parse_args(argv)

    if os.environ.get("CIVICLENS_ENV", "").strip().lower() in PRODUCTION_VALUES:
        print("Refusing: CIVICLENS_ENV says this is production. Demo accounts have a public password.", file=sys.stderr)
        return 2
    store = ComplaintStore(args.db)
    print(f"Target database: {store.db.describe()}")
    if not args.confirm_demo:
        print("Nothing done. Add --confirm-demo to confirm this is a demo or isolated test database.", file=sys.stderr)
        return 2
    if store.backend == "postgres" and not args.allow_postgres:
        print("Nothing done. This is a PostgreSQL database; add --allow-postgres only if it is an isolated test database, "
              "never production.", file=sys.stderr)
        return 2
    try:
        report = seed(store) if args.command == "seed" else remove(store)
    except StoreError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"Demo resident {DEMO_RESIDENT_EMAIL}: {report['resident']}")
    print(f"Demo admin {normalise_username(DEMO_ADMIN_USERNAME)} (role {DEMO_ADMIN_ROLE}): {report['admin']}")
    if args.command == "seed":
        print("These are demonstration accounts with a public password. Do not use them on a production server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
