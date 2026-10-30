"""Cross-origin access for the browser frontend.

Only explicitly listed origins may call the API from a browser. The default is the
local development frontend served by `npm run dev`. Credentials are not allowed
(the API has no cookies or sessions), and a wildcard origin is refused so the list
always stays explicit.

Override with CIVICLENS_CORS_ORIGINS, a comma-separated list, for example:
    CIVICLENS_CORS_ORIGINS=http://localhost:4173,http://127.0.0.1:4173
"""

import os
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

CORS_ORIGINS_ENV = "CIVICLENS_CORS_ORIGINS"
DEFAULT_CORS_ORIGINS = ("http://localhost:4173",)


def _validate_origin(origin: str) -> str:
    parts = urlsplit(origin)
    if origin == "*" or parts.scheme not in ("http", "https") or not parts.netloc or parts.path not in ("", "/") \
            or parts.query or parts.fragment or "@" in parts.netloc:
        raise ValueError(f"Invalid CORS origin {origin!r}: use scheme://host[:port], no wildcard or path")
    return f"{parts.scheme}://{parts.netloc}"


def resolve_cors_origins(origins: list[str] | tuple[str, ...] | None = None) -> list[str]:
    if origins is None:
        raw = os.environ.get(CORS_ORIGINS_ENV)
        origins = [o.strip() for o in raw.split(",") if o.strip()] if raw is not None else list(DEFAULT_CORS_ORIGINS)
    return [_validate_origin(o) for o in origins]


def install_cors(app: FastAPI, origins: list[str]) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "Idempotency-Key"],
        expose_headers=["Location", "Retry-After", "Idempotent-Replayed", "Content-Disposition"],
        max_age=600,
    )
