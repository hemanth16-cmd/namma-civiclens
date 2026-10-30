"""Private evidence file storage.

`EvidenceStorage` is the interface routes depend on; `LocalEvidenceStorage` keeps files on the
local disk (the development default) and `supabase_storage.SupabaseEvidenceStorage` keeps them in a
private Supabase Storage bucket. Each implementation has a `name` ("local" / "supabase") that is recorded on every evidence row,
so a file is always read and deleted from the backend that stored it.

Safety rules kept here:
- Storage keys are generated on the server (128 random bits, hex). A client filename is never used.
- Every key is checked against a strict pattern before it becomes a path, so a key can never point
  outside the storage root.
- Data is written to a temporary file created exclusively in `<root>/tmp`, fsynced, then moved into
  place with an atomic rename. Aborted uploads delete their temporary file.
- The root must not be inside the publicly served frontend directory.
"""

import hashlib
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Protocol

from .store import StoreError

logger = logging.getLogger("civiclens.evidence")

KEY_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STALE_TEMP_SECONDS = 3600


class PendingObject(Protocol):
    size: int
    sha256: str
    content_type: str | None  # set by the upload route once the file type is known

    def write(self, data: bytes) -> None: ...
    def commit(self) -> str: ...
    def abort(self) -> None: ...


class EvidenceStorage(Protocol):
    name: str  # "local" or "supabase": stored on each evidence row

    def initialize(self) -> None: ...
    def begin(self) -> PendingObject: ...
    def delete(self, key: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def stored_bytes(self) -> int: ...
    def open_read(self, key: str): ...


class _LocalPending:
    def __init__(self, storage: "LocalEvidenceStorage", temp_path: Path):
        self._storage = storage
        self._temp = temp_path
        self._file = open(temp_path, "xb")  # exclusive create: never reuses or overwrites a file
        self._hash = hashlib.sha256()
        self.size = 0
        self.content_type = None
        self._done = False

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()

    def write(self, data: bytes) -> None:
        try:
            self._file.write(data)
        except OSError as exc:
            self.abort()
            raise StoreError("Evidence chunk could not be written") from exc
        self._hash.update(data)
        self.size += len(data)

    def commit(self) -> str:
        """Flush to disk and move into place. Returns the new storage key."""
        try:
            self._file.flush()
            os.fsync(self._file.fileno())
            self._file.close()
            key = secrets.token_hex(16)
            final = self._storage.path_for(key)
            final.parent.mkdir(parents=True, exist_ok=True)
            if final.exists():  # 128 random bits: practically impossible, but never overwrite
                raise StoreError("Generated evidence key already exists")
            os.replace(self._temp, final)
            self._done = True
            return key
        except OSError as exc:
            self.abort()
            raise StoreError("Evidence file could not be stored") from exc
        except StoreError:
            self.abort()
            raise

    def abort(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self._file.close()
        except OSError:
            pass
        try:
            self._temp.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not delete a partial evidence file")


class LocalEvidenceStorage:
    name = "local"

    def __init__(self, root: str | os.PathLike, forbidden_roots: list[Path] | tuple[Path, ...] = ()):
        self.root = Path(root).resolve()
        self._objects = self.root / "objects"
        self._tmp = self.root / "tmp"
        self._forbidden = [Path(p).resolve() for p in forbidden_roots]

    def initialize(self) -> None:
        for public in self._forbidden:
            if self.root == public or public in self.root.parents:
                raise StoreError("Evidence storage must not be inside a publicly served directory")
        try:
            self._objects.mkdir(parents=True, exist_ok=True)
            self._tmp.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StoreError("Evidence storage directory could not be created") from exc
        self.remove_stale_temp_files()

    def remove_stale_temp_files(self, older_than: float = STALE_TEMP_SECONDS) -> int:
        """Deletes temporary files left by interrupted uploads (for example after a crash)."""
        removed = 0
        cutoff = time.time() - older_than
        for path in self._tmp.glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    def path_for(self, key: str) -> Path:
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
            raise StoreError("Invalid evidence storage key")
        return self._objects / key[:2] / key

    def begin(self) -> _LocalPending:
        try:
            return _LocalPending(self, self._tmp / f"{secrets.token_hex(16)}.part")
        except OSError as exc:
            raise StoreError("Evidence upload could not be started") from exc

    def delete(self, key: str) -> None:
        try:
            self.path_for(key).unlink(missing_ok=True)
        except OSError as exc:
            raise StoreError("Evidence file could not be deleted") from exc

    def exists(self, key: str) -> bool:
        return self.path_for(key).is_file()

    def open_read(self, key: str):
        """Open a stored file for reading (binary). Access checks are the caller's job; this only
        validates the key. Raises StoreError if the file is missing or unreadable."""
        try:
            return open(self.path_for(key), "rb")
        except OSError as exc:
            raise StoreError("Stored evidence file could not be opened") from exc

    def iter_objects(self):
        """Yields (name, path, size) for every regular file under objects/, including files whose
        name is not a valid key (they are reported, never served)."""
        if not self._objects.is_dir():
            return
        for path in self._objects.rglob("*"):
            if path.is_file() and not path.is_symlink():
                try:
                    yield path.name, path, path.stat().st_size
                except OSError:
                    continue

    def stored_bytes(self) -> int:
        return sum(size for _, _, size in self.iter_objects())

    def temp_files(self):
        """Yields (path, size, mtime) for in-progress or abandoned uploads."""
        for path in self._tmp.glob("*.part") if self._tmp.is_dir() else ():
            try:
                st = path.stat()
                yield path, st.st_size, st.st_mtime
            except OSError:
                continue

    @property
    def tmp_dir(self) -> Path:
        """Private staging folder for in-progress uploads (also used by Supabase uploads)."""
        return self._tmp

    @property
    def objects_dir(self) -> Path:
        return self._objects
