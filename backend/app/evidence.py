"""Evidence upload: POST /api/v1/complaints/{publicId}/evidence.

The request body is multipart/form-data with one `file` part and an optional `capturedAt` text
part. It is parsed as a stream: the upload token and size headers are checked before any body is
read, the file type is decided from its first bytes, limits are enforced while reading, and data
goes to storage in bounded chunks while a SHA-256 checksum is computed. Nothing from the request
(file contents, filename, token, location) is logged.
"""

import asyncio
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request, Response
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .complaints import NOT_FOUND, PUBLIC_ID_PATTERN
from .errors import ApiProblem
from .evidence_storage import EvidenceStorage, PendingObject
from .models import MAX_CLOCK_SKEW, EvidenceReceipt
from .quota import EvidenceQuota
from .ratelimit import enforce
from .store import ComplaintStore, iso, utc_now

logger = logging.getLogger("civiclens.evidence")
router = APIRouter(prefix="/complaints", tags=["evidence"])

MB = 1024 * 1024
HEAD_BYTES = 64          # enough to recognise JPEG, WebM (EBML + DocType) and MP4 (ftyp + brand)
TEXT_FIELD_LIMIT = 64    # bytes for capturedAt
MULTIPART_OVERHEAD = 64 * 1024
MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}


@dataclass(frozen=True)
class UploadSettings:
    # Same limits as dist/services.js (MAX_PHOTO_BYTES, MAX_VIDEO_BYTES).
    max_photo_bytes: int = 25 * MB
    max_video_bytes: int = 200 * MB
    read_timeout_seconds: float = 30.0   # longest pause allowed between body chunks

    @property
    def max_request_bytes(self) -> int:
        return max(self.max_photo_bytes, self.max_video_bytes) + MULTIPART_OVERHEAD

    @classmethod
    def from_env(cls) -> "UploadSettings":
        def num(name, default, cast):
            raw = os.environ.get(name)
            return cast(raw) if raw else default
        return cls(
            max_photo_bytes=num("CIVICLENS_MAX_PHOTO_BYTES", cls.max_photo_bytes, int),
            max_video_bytes=num("CIVICLENS_MAX_VIDEO_BYTES", cls.max_video_bytes, int),
            read_timeout_seconds=num("CIVICLENS_UPLOAD_READ_TIMEOUT", cls.read_timeout_seconds, float),
        )


@dataclass(frozen=True)
class FileKind:
    media_type: str      # "photo" | "video"
    content_type: str
    extensions: frozenset


JPEG = FileKind("photo", "image/jpeg", frozenset({".jpg", ".jpeg"}))
WEBM = FileKind("video", "video/webm", frozenset({".webm"}))
MP4 = FileKind("video", "video/mp4", frozenset({".mp4", ".m4v"}))


def detect_kind(head: bytes) -> FileKind | None:
    """Identify the file from its leading bytes. Browser MIME types and extensions are not trusted."""
    if head[:3] == b"\xff\xd8\xff":
        return JPEG
    if head[:4] == b"\x1a\x45\xdf\xa3" and b"webm" in head[:HEAD_BYTES]:  # EBML header with DocType "webm"
        return WEBM
    if head[4:8] == b"ftyp" and head[8:12] in MP4_BRANDS:
        return MP4
    return None


def _parse_captured_at(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = None
    if dt is None or dt.tzinfo is None:
        raise ApiProblem(422, "validation_error", "capturedAt must be an ISO 8601 time with a timezone.")
    if dt > datetime.now(timezone.utc) + MAX_CLOCK_SKEW:
        raise ApiProblem(422, "validation_error", "capturedAt must not be in the future.")
    return iso(dt)


class _UploadReader:
    """Callbacks for python-multipart's streaming parser. Raises ApiProblem on the first violation."""

    def __init__(self, storage: EvidenceStorage, settings: UploadSettings):
        self.storage = storage
        self.settings = settings
        self.pending: PendingObject | None = None
        self.kind: FileKind | None = None
        self.filename: str | None = None
        self.declared_type: str | None = None
        self.captured_raw = bytearray()
        self.file_seen = False
        self.finished = False
        self._head = bytearray()
        self._part: str | None = None
        self._headers: dict[bytes, bytes] = {}
        self._field = bytearray()
        self._value = bytearray()

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": lambda d, s, e: self._field.extend(d[s:e]),
            "on_header_value": lambda d, s, e: self._value.extend(d[s:e]),
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
            "on_end": self._end,
        }

    def _part_begin(self):
        self._headers, self._part = {}, None

    def _header_end(self):
        self._headers[bytes(self._field).lower()] = bytes(self._value)
        self._field.clear()
        self._value.clear()

    def _headers_finished(self):
        _, params = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = params.get(b"name", b"").decode("utf-8", "replace")
        if name == "file":
            if self.file_seen:
                raise ApiProblem(422, "validation_error", "Send exactly one file.")
            self.file_seen = True
            filename = params.get(b"filename")
            self.filename = filename.decode("utf-8", "replace") if filename else None
            ctype, _ = parse_options_header(self._headers.get(b"content-type", b""))
            self.declared_type = ctype.decode("latin-1").lower() or None
        elif name == "capturedAt":
            pass
        else:
            raise ApiProblem(422, "validation_error", "Unexpected form field. Send only 'file' and optional 'capturedAt'.")
        self._part = name

    def _part_data(self, data: bytes, start: int, end: int):
        chunk = data[start:end]
        if self._part == "capturedAt":
            self.captured_raw.extend(chunk)
            if len(self.captured_raw) > TEXT_FIELD_LIMIT:
                raise ApiProblem(422, "validation_error", "capturedAt is too long.")
            return
        if self.kind is None:
            self._head.extend(chunk)
            if len(self._head) < HEAD_BYTES:
                return
            self._start_file(bytes(self._head))
            chunk = b""
        self._write(chunk)

    def _start_file(self, head: bytes):
        self.kind = detect_kind(head)
        if self.kind is None:
            raise ApiProblem(415, "unsupported_media_type", "Only JPEG photos and WebM or MP4 videos are accepted.")
        self._check_declared()
        self.pending = self.storage.begin()
        self.pending.content_type = self.kind.content_type  # detected from the signature, not the client
        self._write(head)

    def _check_declared(self):
        if self.declared_type and self.declared_type not in (self.kind.content_type, "application/octet-stream"):
            raise ApiProblem(422, "type_mismatch", "The declared file type does not match the file's contents.")
        ext = os.path.splitext(self.filename or "")[1].lower()
        if ext and ext not in self.kind.extensions:
            raise ApiProblem(422, "type_mismatch", "The file extension does not match the file's contents.")

    def _write(self, chunk: bytes):
        if not chunk:
            return
        limit = self.settings.max_photo_bytes if self.kind.media_type == "photo" else self.settings.max_video_bytes
        if self.pending.size + len(chunk) > limit:
            raise ApiProblem(413, "payload_too_large", f"The {self.kind.media_type} is larger than the {limit // MB} MB limit.")
        self.pending.write(chunk)

    def _part_end(self):
        if self._part == "file" and self.kind is None:
            if not self._head:
                raise ApiProblem(422, "empty_file", "The file is empty.")
            self._start_file(bytes(self._head))  # a file shorter than HEAD_BYTES
        self._part = None

    def _end(self):
        self.finished = True


async def _next_chunk(stream, timeout: float) -> bytes | None:
    try:
        return await asyncio.wait_for(anext(stream), timeout)
    except StopAsyncIteration:
        return None


async def receive_upload(request: Request, storage: EvidenceStorage, settings: UploadSettings) -> tuple[_UploadReader, str | None]:
    """Stream the multipart body into storage. On any failure the partial file is deleted."""
    ctype, params = parse_options_header(request.headers.get("content-type", ""))
    boundary = params.get(b"boundary")
    if ctype != b"multipart/form-data" or not boundary:
        raise ApiProblem(415, "unsupported_media_type", "Send the evidence as multipart/form-data.")
    declared_length = request.headers.get("content-length")
    if declared_length and declared_length.isdigit() and int(declared_length) > settings.max_request_bytes:
        raise ApiProblem(413, "payload_too_large", "The upload is larger than the allowed size.")

    reader = _UploadReader(storage, settings)
    parser = MultipartParser(boundary, reader.callbacks())
    received = 0
    stream = request.stream()
    try:
        while True:
            try:
                chunk = await _next_chunk(stream, settings.read_timeout_seconds)
            except asyncio.TimeoutError:
                raise ApiProblem(408, "request_timeout", "The upload stalled and was stopped. Try again.")
            if chunk is None:
                break
            received += len(chunk)
            if received > settings.max_request_bytes:
                raise ApiProblem(413, "payload_too_large", "The upload is larger than the allowed size.")
            if chunk:
                parser.write(chunk)
        parser.finalize()
        if not reader.finished:
            raise ApiProblem(400, "incomplete_upload", "The upload ended early. Try again.")
        if not reader.file_seen:
            raise ApiProblem(422, "validation_error", "Missing the 'file' field.")
        if reader.pending is None or reader.pending.size == 0:
            raise ApiProblem(422, "empty_file", "The file is empty.")
        captured_at = _parse_captured_at(reader.captured_raw.decode("utf-8", "replace"))
        return reader, captured_at
    except MultipartParseError:
        _abort(reader)
        raise ApiProblem(400, "bad_request", "The multipart body could not be read.")
    except ClientDisconnect:
        _abort(reader)
        raise ApiProblem(400, "incomplete_upload", "The upload was interrupted.")
    except BaseException:
        _abort(reader)
        raise


def _abort(reader: _UploadReader):
    if reader.pending is not None:
        reader.pending.abort()


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer ([A-Za-z0-9_\-]{20,200})", value.strip())
    return match.group(1) if match else None


def _receipt(row: dict, status: str) -> dict:
    return {k: row[k] for k in ("evidenceId", "mediaType", "contentType", "byteSize", "sha256", "capturedAt", "uploadedAt")} | {"status": status}


@router.post("/{public_id}/evidence", status_code=201, response_model=EvidenceReceipt)
async def upload_evidence(public_id: str, request: Request, response: Response, background: BackgroundTasks) -> dict:
    store: ComplaintStore = request.app.state.store
    storage: EvidenceStorage = request.app.state.evidence_storage
    settings: UploadSettings = request.app.state.upload_settings
    response.headers["Cache-Control"] = "no-store"

    enforce(request, "evidence_per_ip", "ip")  # before any database or body work
    if not PUBLIC_ID_PATTERN.fullmatch(public_id):
        raise ApiProblem(404, "not_found", NOT_FOUND)
    token = _bearer(request)
    if token is None:
        raise ApiProblem(401, "unauthorized", "An upload token is required.", headers={"WWW-Authenticate": "Bearer"})
    auth = await run_in_threadpool(store.check_upload_token, public_id, token)
    if auth is None:
        raise ApiProblem(404, "not_found", NOT_FOUND)
    if auth["status"] == "expired":
        raise ApiProblem(401, "token_expired", "The upload token has expired. Request a new one with the claim code.", headers={"WWW-Authenticate": "Bearer"})
    if auth["status"] != "ok":
        raise ApiProblem(401, "unauthorized", "The upload token is not valid for this complaint.", headers={"WWW-Authenticate": "Bearer"})
    # Counted only for authorised attempts, so strangers cannot use up a complaint's upload allowance.
    enforce(request, "evidence_per_complaint", public_id)

    declared = request.headers.get("content-length", "")
    declared_size = int(declared) if declared.isdigit() else None
    if declared_size is not None and declared_size > settings.max_request_bytes:
        raise ApiProblem(413, "payload_too_large", "The upload is larger than the allowed size.")
    quota: EvidenceQuota = request.app.state.evidence_quota
    with quota.reserve(declared_size or settings.max_request_bytes) as reservation:  # released on any failure
        reader, captured_at = await receive_upload(request, storage, settings)
        pending, kind = reader.pending, reader.kind
        try:  # until commit, any failure must delete the temporary file
            if kind.media_type != auth["mediaType"]:
                raise ApiProblem(422, "media_type_mismatch", f"This complaint was reported as a {auth['mediaType']}, but the file is a {kind.media_type}.")
            existing = await run_in_threadpool(store.get_evidence, auth["pk"])
            if existing is not None:
                pending.abort()
                return _same_or_conflict(existing, pending.sha256, response)
            key = await run_in_threadpool(pending.commit)
        except BaseException:
            pending.abort()
            raise
        meta = {
            "evidenceId": "ev_" + secrets.token_urlsafe(16), "storageKey": key, "mediaType": kind.media_type,
            "contentType": kind.content_type, "byteSize": pending.size, "sha256": pending.sha256,
            "capturedAt": captured_at, "uploadedAt": utc_now(), "storageBackend": storage.name,
        }
        try:
            outcome, row = await run_in_threadpool(store.add_evidence, auth["pk"], meta)
        except BaseException:
            await run_in_threadpool(storage.delete, key)  # no database row may point at a missing file, and vice versa
            raise
        if outcome == "exists":  # a concurrent retry won the race
            await run_in_threadpool(storage.delete, key)
            return _same_or_conflict(row, meta["sha256"], response)
        reservation.commit(pending.size)
        background.add_task(request.app.state.notifier.evidence_uploaded, public_id, kind.media_type)
        # After the response: AI analysis reads the committed file from private storage (app/ai/service.py).
        background.add_task(request.app.state.ai.evidence_added, row["evidenceId"])
        return _receipt(row, "uploaded")


def _same_or_conflict(existing: dict, sha256: str, response: Response) -> dict:
    if existing["sha256"] == sha256:
        response.status_code = 200
        return _receipt(existing, "already_uploaded")
    raise ApiProblem(409, "evidence_exists", "Different evidence has already been uploaded for this complaint.")
