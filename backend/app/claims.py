"""Private claim codes and public reference IDs.

Claim codes are shown once, on the creation receipt, and stored only as a
salted scrypt hash. Nothing in this module logs a plaintext code.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

# Same alphabet as referenceId() in dist/services.js (no I, O, 0 or 1).
ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
IST = timezone(timedelta(hours=5, minutes=30))

# scrypt cost: n=2^14, r=8, p=1 uses about 16 MB and is the standard interactive setting.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DKLEN, _SALT_BYTES = 2**14, 8, 1, 32, 16


def new_public_id(now: datetime | None = None) -> str:
    year = (now or datetime.now(IST)).astimezone(IST).year
    return f"NCL-{year}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(6))


def new_claim_code() -> str:
    """A random UUID (122 random bits from the OS CSPRNG), the same format the browser prototype issues."""
    return str(uuid.uuid4())


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_claim_code(code: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(code.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def new_upload_token() -> str:
    """A short-lived bearer token for evidence upload (256 random bits, URL-safe)."""
    return secrets.token_urlsafe(32)


def hash_upload_token(token: str) -> str:
    """Upload tokens are random and high-entropy, so a fast unsalted SHA-256 is sufficient for
    storage; only the hash is kept, so a database leak does not reveal usable tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def upload_token_matches(token: str, stored_hash: str | None) -> bool:
    if not stored_hash or not isinstance(token, str):
        return False
    return hmac.compare_digest(hash_upload_token(token), stored_hash)


def verify_claim_code(code: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(code.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)
