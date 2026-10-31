"""FastAPI application factory.

Run locally from backend/:
    .venv\\Scripts\\python -m uvicorn app.main:app --reload --port 8000
"""

import os
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, FastAPI

from . import API_VERSION, APP_VERSION, SERVICE_NAME
from .cases_api import router as cases_router
from .complaints import router as complaints_router
from .cors import install_cors, resolve_cors_origins
from .errors import install_error_handlers
from .evidence import UploadSettings
from .evidence import router as evidence_router
from .auth import session_ttl
from .resident_auth import session_ttl as resident_session_ttl_from_env
from .residents_api import router as residents_router
from .owner_evidence import router as owner_evidence_router
from .evidence_storage import EvidenceStorage, LocalEvidenceStorage
from .notifications import MAX_FAILURES, Notifier
from .notifications_api import router as notifications_router
from .push import DEFAULT_ENDPOINT_HOSTS, PushSender, PushSettings, WebPushSender
from .supabase_storage import SupabaseEvidenceStorage, SupabaseSettings, selected_backend
from .staff_api import router as staff_router
from .quota import EvidenceQuota, quota_from_env
from .ratelimit import InMemoryRateLimiter, RateLimiter, RateLimitSettings
from .store import DEFAULT_REPLAY_WINDOW, DEFAULT_UPLOAD_TOKEN_TTL, ComplaintStore

API_PREFIX = f"/api/{API_VERSION}"
BACKEND_DIR = Path(__file__).resolve().parents[1]
# The frontend's publicly served folder. Evidence storage is refused anywhere inside it.
PUBLIC_DIR = BACKEND_DIR.parent / "dist"
EVIDENCE_DIR_ENV = "CIVICLENS_EVIDENCE_DIR"
DEFAULT_EVIDENCE_DIR = BACKEND_DIR / "data" / "evidence"
TOKEN_TTL_ENV = "CIVICLENS_UPLOAD_TOKEN_TTL_SECONDS"
REPLAY_WINDOW_ENV = "CIVICLENS_IDEMPOTENCY_REPLAY_SECONDS"


def health() -> dict:
    """Liveness check. Reports only that the process is serving requests;
    it does not check the database or evidence storage."""
    return {"status": "ok", "service": SERVICE_NAME, "api_version": API_VERSION, "version": APP_VERSION}


def create_app(
    db_path: str | os.PathLike | None = None,
    store: ComplaintStore | None = None,
    cors_origins: list[str] | None = None,
    evidence_dir: str | os.PathLike | None = None,
    evidence_storage: EvidenceStorage | None = None,
    upload_settings: UploadSettings | None = None,
    rate_limit_settings: RateLimitSettings | None = None,
    rate_limiter: RateLimiter | None = None,
    evidence_quota: EvidenceQuota | None = None,
    replay_window: timedelta | None = None,
    staff_session_ttl: timedelta | None = None,
    resident_session_ttl: timedelta | None = None,
    push_settings: PushSettings | None = None,
    push_sender: PushSender | None = None,
) -> FastAPI:
    """Build the app. The database is chosen by DATABASE_BACKEND: sqlite (default; the file is `db_path`,
    else CIVICLENS_DB_PATH, else backend/data/civiclens.sqlite3) or postgres (DATABASE_URL; `db_path`
    is then ignored). Evidence files go to `evidence_dir`, else
    CIVICLENS_EVIDENCE_DIR, else backend/data/evidence. The schema and storage folders
    are created at startup, so importing this module never touches the disk. Browser
    origins come from `cors_origins`, else CIVICLENS_CORS_ORIGINS, else http://localhost:4173."""
    ttl_raw = os.environ.get(TOKEN_TTL_ENV)
    ttl = timedelta(seconds=int(ttl_raw)) if ttl_raw else DEFAULT_UPLOAD_TOKEN_TTL
    # Database: DATABASE_BACKEND=sqlite (default; db_path or CIVICLENS_DB_PATH) or postgres (DATABASE_URL).
    owns_store = store is None
    store = store or ComplaintStore(db_path, upload_token_ttl=ttl)
    upload_settings = upload_settings or UploadSettings.from_env()
    # The local store is always available, so evidence stored on disk before Supabase Storage was
    # enabled stays readable. New uploads go to the active backend:
    # EVIDENCE_STORAGE_BACKEND=local (default) or supabase.
    if isinstance(evidence_storage, LocalEvidenceStorage):
        local_storage = evidence_storage
    else:
        local_storage = LocalEvidenceStorage(
            evidence_dir or os.environ.get(EVIDENCE_DIR_ENV) or DEFAULT_EVIDENCE_DIR, forbidden_roots=[PUBLIC_DIR]
        )
    if evidence_storage is None and selected_backend() == "supabase":
        # Misconfiguration fails here, when the app is built, with a message naming the variable.
        # Uploads are staged in the local store's private tmp/ folder (cleaned at startup).
        evidence_storage = SupabaseEvidenceStorage(
            SupabaseSettings.from_env(), temp_dir=local_storage.tmp_dir,
            max_object_bytes=max(upload_settings.max_photo_bytes, upload_settings.max_video_bytes),
        )
    evidence_storage = evidence_storage or local_storage
    evidence_storages = {local_storage.name: local_storage, evidence_storage.name: evidence_storage}
    # Explicit `is None` checks: an empty limiter has len() == 0 and would be falsy.
    if rate_limit_settings is None:
        rate_limit_settings = RateLimitSettings.from_env()
    if rate_limiter is None:
        rate_limiter = InMemoryRateLimiter()
    if evidence_quota is None:
        evidence_quota = EvidenceQuota(quota_from_env())
    if replay_window is None:
        replay_raw = os.environ.get(REPLAY_WINDOW_ENV)
        replay_window = timedelta(seconds=int(replay_raw)) if replay_raw else DEFAULT_REPLAY_WINDOW
    if staff_session_ttl is None:
        staff_session_ttl = session_ttl()
    if resident_session_ttl is None:
        resident_session_ttl = resident_session_ttl_from_env()
    origins = resolve_cors_origins(cors_origins)
    # Web Push: VAPID_* variables (push.py). Without keys, notifications are stored but not pushed.
    # Tests pass push_settings and a fake push_sender.
    if push_settings is None:
        push_settings = PushSettings.from_env()
    if push_sender is None and push_settings is not None:
        push_sender = WebPushSender(push_settings)
    notifier = Notifier(store, push_sender if push_settings is not None else None)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store.initialize()
        for storage in evidence_storages.values():
            storage.initialize()  # Supabase: key, bucket privacy and limits are checked here; failures stop startup
        # Stored usage survives restarts: the larger of the database total and the bytes actually stored
        # across all configured backends.
        evidence_quota.load(store.total_evidence_bytes(), sum(s.stored_bytes() for s in evidence_storages.values()))
        store.prune_push_subscriptions(MAX_FAILURES)  # expired or repeatedly failing devices
        yield
        if owns_store:
            store.close()  # returns pooled PostgreSQL connections; no-op for SQLite

    app = FastAPI(
        title="Namma CivicLens API",
        version=APP_VERSION,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.evidence_storage = evidence_storage     # where new uploads go
    app.state.evidence_storages = evidence_storages   # every backend that may hold existing evidence
    app.state.upload_settings = upload_settings
    app.state.rate_limit_settings = rate_limit_settings
    app.state.rate_limiter = rate_limiter
    app.state.evidence_quota = evidence_quota
    app.state.replay_window = replay_window
    app.state.staff_session_ttl = staff_session_ttl
    app.state.resident_session_ttl = resident_session_ttl
    app.state.push_settings = push_settings
    app.state.push_endpoint_hosts = push_settings.endpoint_hosts if push_settings else DEFAULT_ENDPOINT_HOSTS
    app.state.notifier = notifier
    install_error_handlers(app)
    install_cors(app, origins)

    router = APIRouter(prefix=API_PREFIX)
    router.add_api_route("/health", health, methods=["GET"], tags=["system"])
    router.include_router(complaints_router)
    router.include_router(evidence_router)
    router.include_router(staff_router)
    router.include_router(residents_router)
    router.include_router(owner_evidence_router)
    router.include_router(cases_router)
    router.include_router(notifications_router)
    app.include_router(router)
    return app


app = create_app()
