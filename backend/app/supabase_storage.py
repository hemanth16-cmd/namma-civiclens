"""Private Supabase Storage evidence storage.

Selected with EVIDENCE_STORAGE_BACKEND=supabase. Implements the same EvidenceStorage interface as
LocalEvidenceStorage, so upload validation, authorisation, quota, idempotency and audit logging in
the routes are unchanged.

Configuration (environment; backend only, never hard-coded, never sent to the frontend):
  SUPABASE_URL               project URL, e.g. https://<project-ref>.supabase.co
  SUPABASE_SERVICE_ROLE_KEY  service-role (secret) key; bypasses row-level security, so it must
                             stay on the server
  SUPABASE_STORAGE_BUCKET    an existing PRIVATE bucket

Uses the official Supabase Storage client (`storage3`, the storage part of supabase-py) for bucket
checks, uploads, existence checks, listing and deletion. Downloads are the one exception: the SDK's
download() returns the whole object in memory, so evidence is streamed from the same endpoint
(GET /object/{bucket}/{path}) with the SDK's own authenticated HTTP client instead.

Safety rules kept here:
- Object names are server-generated: evidence/<first 2 hex>/<32 hex>. The 32-hex key is what the
  database stores; object paths never leave the server.
- Startup refuses a public bucket, a missing bucket, a wrong key, and a bucket whose size or MIME
  limits would reject evidence the API accepts.
- No public or signed URLs: staff evidence is streamed through the API, which logs each view first.
- An upload is staged in the private local tmp/ folder, streamed to Supabase, then confirmed with an
  info request. Only a confirmed object counts as committed; otherwise it is removed and the upload
  fails.
- Errors become StoreError carrying a status/error code only: never the key, URLs or object paths.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from storage3 import SyncStorageClient
from storage3.exceptions import StorageApiError

from .evidence_storage import KEY_PATTERN
from .store import StoreError

logger = logging.getLogger("civiclens.evidence.supabase")
logging.getLogger("httpx").setLevel(logging.WARNING)  # request lines would include object paths

BACKEND_ENV = "EVIDENCE_STORAGE_BACKEND"
URL_ENV = "SUPABASE_URL"
KEY_ENV = "SUPABASE_SERVICE_ROLE_KEY"
BUCKET_ENV = "SUPABASE_STORAGE_BUCKET"
KEY_PREFIX = "evidence/"
READ_CHUNK = 64 * 1024
LIST_PAGE = 1000
EVIDENCE_MIME_TYPES = ("image/jpeg", "video/webm", "video/mp4")
BUCKET_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class StorageConfigError(ValueError):
    """Evidence storage is misconfigured. The message names what to fix; it never contains secrets."""


def selected_backend() -> str:
    value = os.environ.get(BACKEND_ENV, "local").strip().lower() or "local"
    if value not in ("local", "supabase"):
        raise StorageConfigError(f'{BACKEND_ENV} must be "local" or "supabase", not {value!r}.')
    return value


def _jwt_role(key: str) -> str | None:
    """The `role` claim of a legacy JWT API key, read without verification (only to catch the anon key)."""
    parts = key.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("role")
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    service_role_key: str = field(repr=False)
    bucket: str

    def __post_init__(self):
        parts = urlsplit(self.url or "")
        local = parts.hostname in LOCAL_HOSTS
        if not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") or parts.query \
                or not (parts.scheme == "https" or (parts.scheme == "http" and local)):
            raise StorageConfigError(f"{URL_ENV} must be the project URL, e.g. https://<project-ref>.supabase.co "
                                     "(http is allowed only for a local Supabase on localhost).")
        if not self.service_role_key:
            raise StorageConfigError(f"{BACKEND_ENV}=supabase needs {KEY_ENV} (Project Settings → API keys).")
        if any(c.isspace() for c in self.service_role_key):
            raise StorageConfigError(f"{KEY_ENV} contains whitespace; copy it again from the Supabase dashboard.")
        if self.service_role_key.startswith("sb_publishable_") or _jwt_role(self.service_role_key) == "anon":
            raise StorageConfigError(f"{KEY_ENV} is the public anon/publishable key; use the service-role (secret) key.")
        if not self.bucket:
            raise StorageConfigError(f"{BACKEND_ENV}=supabase needs {BUCKET_ENV} (the name of an existing private bucket).")
        if not BUCKET_PATTERN.fullmatch(self.bucket):
            raise StorageConfigError(f"{BUCKET_ENV}={self.bucket!r} is not a valid bucket name.")

    @property
    def storage_url(self) -> str:
        return self.url.rstrip("/") + "/storage/v1"

    @classmethod
    def from_env(cls) -> "SupabaseSettings":
        return cls(
            url=os.environ.get(URL_ENV, "").strip(),
            service_role_key=os.environ.get(KEY_ENV, "").strip(),
            bucket=os.environ.get(BUCKET_ENV, "").strip(),
        )


def _code(exc: Exception) -> str:
    """A safe identifier for a Supabase error: HTTP status and error code, or the exception class."""
    if isinstance(exc, StorageApiError):
        return f"{exc.status} {exc.code}".strip()
    if isinstance(exc, httpx.HTTPStatusError):
        return str(exc.response.status_code)
    return type(exc).__name__


def _is_not_found(exc: Exception) -> bool:
    if isinstance(exc, StorageApiError):
        return str(exc.status) == "404" or str(exc.code).lower() in ("not_found", "nosuchkey", "object not found")
    return False


def _transport() -> httpx.BaseTransport:
    """HTTP transport for the Supabase client: retries failed connections twice. Tests replace this."""
    return httpx.HTTPTransport(retries=2)


def _mime_allowed(allowed: list[str] | None, mime: str) -> bool:
    if not allowed:
        return True
    family = mime.split("/")[0] + "/*"
    return any(a in (mime, family, "*/*", "*") for a in allowed)


class _SupabasePending:
    def __init__(self, storage: "SupabaseEvidenceStorage"):
        self._storage = storage
        self._path = storage.temp_dir / f"{secrets.token_hex(16)}.part"
        self._file = open(self._path, "xb")  # private tmp/ folder, cleaned at startup if abandoned
        self._hash = hashlib.sha256()
        self.size = 0
        self.content_type: str | None = None
        self._done = False

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def write(self, data: bytes) -> None:
        try:
            self._file.write(data)
        except OSError as exc:
            self.abort()
            raise StoreError("Evidence chunk could not be buffered") from exc
        self._hash.update(data)
        self.size += len(data)

    def commit(self) -> str:
        """Stream the staged file to Supabase and confirm the object exists. Returns the key."""
        if self._done:
            raise StoreError("Upload already finished")
        key = secrets.token_hex(16)
        name = self._storage.object_name(key)
        try:
            self._file.close()
            # The SDK closes a handle it opened only on success, so it gets our handle and we always
            # close it ourselves; otherwise a failed upload would leave the staged file locked (Windows).
            with open(self._path, "rb") as staged:
                self._storage.bucket_api.upload(name, staged, {
                    "content-type": self.content_type or "application/octet-stream",
                    "cache-control": "no-store",
                    "upsert": "false",
                    "metadata": {"sha256": self.sha256},
                })
        except Exception as exc:  # StorageApiError, httpx network errors
            self.abort()
            logger.error("Supabase evidence upload failed (%s)", _code(exc))
            raise StoreError(f"Evidence could not be stored in Supabase Storage ({_code(exc)})") from exc
        self.abort()  # removes the staged copy; the object is in Supabase now
        if not self._storage.confirm(name, self.size):
            self._storage.delete(key, quiet=True)
            raise StoreError("Evidence upload to Supabase Storage could not be confirmed")
        return key

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._file.close()
        except OSError:
            pass
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete a staged evidence file")


class _SupabaseReader:
    """File-like wrapper over a streamed HTTP response: read(n) and close(), as the staff route expects."""

    def __init__(self, response: httpx.Response):
        self._response = response
        self._chunks = response.iter_bytes(READ_CHUNK)

    def read(self, size: int = READ_CHUNK) -> bytes:
        try:
            return next(self._chunks, b"")
        except Exception as exc:
            logger.error("Supabase evidence read failed mid-stream (%s)", _code(exc))
            raise StoreError("Evidence could not be read from Supabase Storage") from exc

    def close(self) -> None:
        self._response.close()


class SupabaseEvidenceStorage:
    name = "supabase"

    def __init__(self, settings: SupabaseSettings, temp_dir: str | os.PathLike, max_object_bytes: int):
        self.settings = settings
        self.bucket = settings.bucket
        self.temp_dir = Path(temp_dir)
        self.max_object_bytes = max_object_bytes
        key = settings.service_role_key
        self._headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        self._http = httpx.Client(transport=_transport(), timeout=httpx.Timeout(30.0, connect=5.0))
        self._client = SyncStorageClient(settings.storage_url + "/", self._headers, http_client=self._http)
        self.bucket_api = self._client.from_(settings.bucket)

    def object_name(self, key: str) -> str:
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise StoreError("Invalid evidence storage key")
        return f"{KEY_PREFIX}{key[:2]}/{key}"

    def initialize(self) -> None:
        """Check the key, the bucket and the bucket's privacy and limits at startup."""
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        try:
            bucket = self._client.get_bucket(self.bucket)
        except Exception as exc:
            code = _code(exc)
            status = str(getattr(exc, "status", ""))
            if _is_not_found(exc) or "not found" in code.lower():
                hint = f"bucket {self.bucket!r} does not exist (create it as a private bucket first)"
            elif status in ("400", "401", "403") or "jwt" in str(getattr(exc, "message", "")).lower():
                hint = "the service-role key was rejected (check SUPABASE_SERVICE_ROLE_KEY and SUPABASE_URL)"
            elif isinstance(exc, httpx.TransportError):
                hint = "the Supabase project could not be reached (check SUPABASE_URL and the network)"
            else:
                hint = f"the bucket check failed ({code})"
            raise StoreError(f"Supabase evidence storage is unavailable: {hint}.") from exc
        if bucket.public:
            raise StoreError(f"Supabase bucket {self.bucket!r} is public. Evidence must stay private: turn off "
                             "'Public bucket' in Storage → bucket settings, then restart.")
        if bucket.file_size_limit and bucket.file_size_limit < self.max_object_bytes:
            raise StoreError(
                f"Supabase bucket {self.bucket!r} accepts files up to {bucket.file_size_limit} bytes, but the API accepts "
                f"evidence up to {self.max_object_bytes} bytes. Raise the bucket's file size limit, or lower "
                "CIVICLENS_MAX_VIDEO_BYTES / CIVICLENS_MAX_PHOTO_BYTES to match.")
        missing = [m for m in EVIDENCE_MIME_TYPES if not _mime_allowed(bucket.allowed_mime_types, m)]
        if missing:
            raise StoreError(f"Supabase bucket {self.bucket!r} does not allow {', '.join(missing)}; "
                             "add them to its allowed MIME types (or leave the list empty).")

    def confirm(self, name: str, expected_size: int) -> bool:
        """True if the object exists and, when Supabase reports a size, it matches."""
        try:
            info = self.bucket_api.info(name)
        except Exception as exc:
            logger.error("Supabase evidence upload could not be confirmed (%s)", _code(exc))
            return False
        size = info.get("size") if isinstance(info, dict) else None
        if size is None and isinstance(info, dict) and isinstance(info.get("metadata"), dict):
            size = info["metadata"].get("size")
        return size is None or int(size) == expected_size

    def begin(self) -> _SupabasePending:
        return _SupabasePending(self)

    def delete(self, key: str, quiet: bool = False) -> None:
        try:
            self.bucket_api.remove([self.object_name(key)])
        except StoreError:
            raise
        except Exception as exc:
            logger.error("Supabase evidence delete failed (%s)", _code(exc))
            if not quiet:
                raise StoreError(f"Evidence could not be deleted from Supabase Storage ({_code(exc)})") from exc

    def exists(self, key: str) -> bool:
        try:
            return self.bucket_api.exists(self.object_name(key))
        except StoreError:
            raise
        except Exception as exc:
            raise StoreError(f"Evidence could not be checked in Supabase Storage ({_code(exc)})") from exc

    def open_read(self, key: str) -> _SupabaseReader:
        url = f"{self.settings.storage_url}/object/{quote(self.bucket)}/{quote(self.object_name(key))}"
        try:
            response = self._http.send(self._http.build_request("GET", url, headers=self._headers), stream=True)
        except Exception as exc:
            logger.error("Supabase evidence read failed (%s)", _code(exc))
            raise StoreError(f"Evidence could not be read from Supabase Storage ({_code(exc)})") from exc
        if response.status_code != 200:
            status = response.status_code
            response.read()
            response.close()
            logger.error("Supabase evidence read failed (%s)", status)
            raise StoreError("Stored evidence object is missing" if status in (400, 404) else f"Evidence could not be read from Supabase Storage ({status})")
        return _SupabaseReader(response)

    def stored_bytes(self) -> int:
        """Total size of evidence objects (evidence/<2 hex>/<32 hex>), listed folder by folder at startup."""
        try:
            total = 0
            for folder in self._list(KEY_PREFIX.rstrip("/")):
                if folder.get("id") is None:  # a folder entry
                    total += sum(int((f.get("metadata") or {}).get("size", 0)) for f in self._list(f"{KEY_PREFIX}{folder['name']}") if f.get("id"))
            return total
        except Exception as exc:
            raise StoreError(f"Supabase evidence usage could not be read ({_code(exc)})") from exc

    def _list(self, path: str) -> list[dict]:
        entries, offset = [], 0
        while True:
            page = self.bucket_api.list(path, {"limit": LIST_PAGE, "offset": offset})
            entries.extend(page)
            if len(page) < LIST_PAGE:
                return entries
            offset += LIST_PAGE
