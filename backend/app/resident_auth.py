"""Resident authentication: accounts, bearer sessions and the `current_resident` dependency.

Built like staff authentication (auth.py) but in its own domain: resident accounts and sessions live in
their own tables, so a resident token never authenticates on /staff endpoints and a staff token never
authenticates as a resident. Passwords use the same scrypt construction; sign-in returns a random
256-bit bearer token of which only the SHA-256 hash is stored, with an expiry (default 8 hours,
CIVICLENS_RESIDENT_SESSION_SECONDS). The browser keeps it in memory only.

A complaint's owner is taken from a validated resident session, never from the request body.
Anonymous reporting needs no account and is unaffected. Development-grade: no email verification,
password reset or MFA.
"""

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request

from .auth import _DUMMY_HASH, bearer_token, hash_password, password_problem, verify_password
from .claims import hash_upload_token
from .errors import ApiProblem
from .store import ComplaintStore, iso

SESSION_TTL_ENV = "CIVICLENS_RESIDENT_SESSION_SECONDS"
DEFAULT_SESSION_TTL = timedelta(hours=8)
EMAIL_PATTERN = re.compile(r"^[^@\s]{1,64}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
UNAUTHORIZED = "Sign in to your resident account to continue. Your session may have expired."

__all__ = ["hash_password", "password_problem", "normalise_email", "email_problem", "session_ttl", "sign_in",
           "current_resident", "optional_resident", "resident_identity"]


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def email_problem(email: str) -> str | None:
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return "Enter a valid email address."
    return None


def session_ttl() -> timedelta:
    raw = os.environ.get(SESSION_TTL_ENV)
    return timedelta(seconds=int(raw)) if raw else DEFAULT_SESSION_TTL


def resident_identity(row: dict) -> dict:
    """The public shape of a resident account (no pk, hash or timestamps beyond creation)."""
    return {"residentId": row["resident_id"], "email": row["email"], "displayName": row["display_name"],
            "demo": bool(row.get("is_demo")), "createdAt": row.get("created_at")}


def new_session(store: ComplaintStore, account: dict, ttl: timedelta) -> dict:
    token = secrets.token_urlsafe(32)
    expires = iso(datetime.now(timezone.utc) + ttl)
    store.create_resident_session(account["pk"], hash_upload_token(token), expires)
    return {"token": token, "expiresAt": expires, "resident": resident_identity(account)}


def sign_in(store: ComplaintStore, email: str, password: str, ttl: timedelta) -> dict | None:
    """Returns {"token", "expiresAt", "resident"} or None. Always runs one scrypt verification, so an
    unknown email takes about as long as a wrong password."""
    account = store.get_resident(normalise_email(email))
    ok = verify_password(password, account["password_hash"] if account else _DUMMY_HASH)
    if not account or not ok or account["disabled_at"] is not None:
        return None
    return new_session(store, account, ttl)


def _session(request: Request) -> dict | None:
    token = bearer_token(request)
    if not token:
        return None
    row = request.app.state.store.get_resident_session(hash_upload_token(token))
    if row is None:
        return None
    return {"pk": row["pk"], "residentId": row["resident_id"], "email": row["email"], "displayName": row["display_name"],
            "demo": bool(row["is_demo"]), "createdAt": row["created_at"], "sessionExpiresAt": row["expires_at"],
            "tokenHash": hash_upload_token(token)}


def current_resident(request: Request) -> dict:
    """FastAPI dependency: the signed-in resident, or 401. A staff token is not a resident session."""
    resident = _session(request)
    if resident is None:
        raise ApiProblem(401, "unauthorized", UNAUTHORIZED, headers={"WWW-Authenticate": "Bearer"})
    return resident


def optional_resident(request: Request) -> dict | None:
    """For endpoints open to everyone (POST /complaints): None without an Authorization header; the
    resident when a valid resident token is sent; 401 for any other bearer token (expired, revoked,
    or a staff token), so a stale sign-in never silently turns into an anonymous report."""
    if not request.headers.get("authorization"):
        return None
    return current_resident(request)
