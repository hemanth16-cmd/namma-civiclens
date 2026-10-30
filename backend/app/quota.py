"""Total evidence-storage quota.

Before an upload body is read, the request reserves capacity (its Content-Length, or the largest
file the endpoint accepts when the length is unknown). Reservations count against the quota
together with stored evidence, so concurrent uploads cannot jointly exceed it. On success the
reservation becomes the file's real size; on any failure or cancellation it is released.

Stored usage is loaded at startup as the larger of the database total (evidence.byte_size) and the
bytes actually present in storage, so missing or inconsistent metadata errs on the safe side.
Partial uploads are never counted as stored evidence.

Policy: a request that would exceed the quota is rejected with 507 storage_quota_exceeded. Nothing
is ever deleted to make room.
"""

import os
import threading

from .errors import ApiProblem

QUOTA_ENV = "CIVICLENS_EVIDENCE_QUOTA_BYTES"
DEFAULT_QUOTA_BYTES = 2 * 1024 ** 3   # 2 GB for development


def quota_from_env() -> int:
    raw = os.environ.get(QUOTA_ENV)
    return int(raw) if raw else DEFAULT_QUOTA_BYTES


class Reservation:
    def __init__(self, quota: "EvidenceQuota", size: int):
        self._quota = quota
        self.size = size
        self._open = True

    def commit(self, actual_size: int) -> None:
        """The file is stored: count its real size and release the rest of the reservation."""
        if self._open:
            self._open = False
            self._quota._settle(self.size, actual_size)

    def release(self) -> None:
        if self._open:
            self._open = False
            self._quota._settle(self.size, 0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()  # no-op after commit
        return False


class EvidenceQuota:
    def __init__(self, limit_bytes: int = DEFAULT_QUOTA_BYTES):
        if limit_bytes < 0:
            raise ValueError("Quota must not be negative")
        self.limit_bytes = limit_bytes
        self._used = 0
        self._reserved = 0
        self._lock = threading.Lock()

    def load(self, recorded_bytes: int, on_disk_bytes: int) -> None:
        with self._lock:
            self._used = max(int(recorded_bytes), int(on_disk_bytes), 0)

    @property
    def used(self) -> int:
        return self._used

    @property
    def reserved(self) -> int:
        return self._reserved

    def reserve(self, size: int) -> Reservation:
        with self._lock:
            if self._used + self._reserved + size > self.limit_bytes:
                raise ApiProblem(507, "storage_quota_exceeded",
                                 "Evidence storage is full, so this upload cannot be accepted right now. The complaint itself is saved.")
            self._reserved += size
        return Reservation(self, size)

    def _settle(self, reserved: int, stored: int) -> None:
        with self._lock:
            self._reserved -= reserved
            self._used += stored

    def release_stored(self, size: int) -> None:
        """A stored file was removed (for example after a failed database insert)."""
        with self._lock:
            self._used = max(self._used - size, 0)
