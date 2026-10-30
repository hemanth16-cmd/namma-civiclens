"""Staff authentication: password hashing, bearer sessions and the `current_staff` dependency.

- Passwords use the same scrypt construction as claim codes (claims.py), with a fresh salt each.
- Sign-in returns a random 256-bit bearer token; only its SHA-256 hash is stored, with an expiry
  (default 8 hours, CIVICLENS_STAFF_SESSION_SECONDS). The browser keeps it in memory only.
- Unknown usernames are checked against a dummy hash so they take about as long as wrong passwords.
- Disabling an account or changing its password revokes its open sessions (store.py).
This is development-grade authentication for one role ("official"); there is no MFA, no password
reset by email and no account lockout beyond rate limits.
"""

import os
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Request

from .claims import hash_claim_code, hash_upload_token, verify_claim_code
from .errors import ApiProblem
from .store import ComplaintStore, iso

SESSION_TTL_ENV = "CIVICLENS_STAFF_SESSION_SECONDS"
DEFAULT_SESSION_TTL = timedelta(hours=8)
USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,64}$")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
_DUMMY_HASH = hash_claim_code(secrets.token_urlsafe(24))
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_\-]{20,200})")


def hash_password(password: str) -> str:
    return hash_claim_code(password)


def verify_password(password: str, stored: str) -> bool:
    return verify_claim_code(password, stored)


def normalise_username(username: str) -> str:
    return (username or "").strip().lower()


def password_problem(password: str) -> str | None:
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
    return None


def session_ttl() -> timedelta:
    raw = os.environ.get(SESSION_TTL_ENV)
    return timedelta(seconds=int(raw)) if raw else DEFAULT_SESSION_TTL


def sign_in(store: ComplaintStore, username: str, password: str, ttl: timedelta) -> dict | None:
    """Returns {"token", "expiresAt", "staff"} or None. Always runs one scrypt verification."""
    account = store.get_staff(normalise_username(username))
    ok = verify_password(password, account["password_hash"] if account else _DUMMY_HASH)
    if not account or not ok or account["disabled_at"] is not None:
        return None
    token = secrets.token_urlsafe(32)
    expires = iso(datetime.now(timezone.utc) + ttl)
    store.create_staff_session(account["pk"], hash_upload_token(token), expires)
    return {"token": token, "expiresAt": expires,
            "staff": {"username": account["username"], "displayName": account["display_name"], "role": account["role"]}}


def bearer_token(request: Request) -> str | None:
    match = _BEARER.fullmatch(request.headers.get("authorization", "").strip())
    return match.group(1) if match else None


def current_staff(request: Request) -> dict:
    """FastAPI dependency: the signed-in staff member, or 401."""
    token = bearer_token(request)
    session = request.app.state.store.get_staff_session(hash_upload_token(token)) if token else None
    if session is None:
        raise ApiProblem(401, "unauthorized", "Sign in as CivicLens staff to continue. Your session may have expired.",
                         headers={"WWW-Authenticate": "Bearer"})
    return {"pk": session["pk"], "username": session["username"], "displayName": session["display_name"],
            "role": session["role"], "sessionExpiresAt": session["expires_at"], "tokenHash": hash_upload_token(token)}
