"""Disk manager: the only component that touches the database file.

The database file is an array of fixed-size pages::

    file offset = page_id * PAGE_SIZE

Responsibilities:

* read/write whole pages (never partial pages)
* verify a CRC32 checksum on every read and stamp one on every write, so
  corruption is detected instead of silently returning garbage
* allocate new page ids by extending the file
* keep page 0 (the file header) with the magic number, format version and the
  LSN high-water mark recorded at the last checkpoint
* hold an OS-level lock so two processes cannot open the same file
"""

from __future__ import annotations

import logging
import os
import struct
from typing import BinaryIO

from pydb.config import DB_FORMAT_VERSION, DB_MAGIC, HEADER_PAGE_ID, PAGE_SIZE
from pydb.errors import DatabaseLockedError, PageCorruptionError, StorageError
from pydb.storage.page import COMMON_HEADER_SIZE, Page, PageType, stamp_checksum, verify_checksum

log = logging.getLogger(__name__)

# Body of page 0, right after the common header.
FILE_HEADER = struct.Struct("<4sHIQ")  # magic, version, page_size, checkpoint_lsn


class _FileLock:
    """Cross-platform exclusive lock on ``<db>.lock`` (released by the OS if we crash)."""

    def __init__(self, path: str) -> None:
        self.path = path + ".lock"
        self._fh: BinaryIO | None = None

    def acquire(self) -> None:
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise DatabaseLockedError(f"database is already open in another process ({self.path})") from exc
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best effort
            pass
        self._fh.close()
        self._fh = None


class DiskManager:
    """Page-granular access to a single database file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = _FileLock(path)
        self._lock.acquire()
        self.created = not os.path.exists(path) or os.path.getsize(path) == 0
        try:
            self._fh: BinaryIO = open(path, "r+b" if not self.created else "w+b")
        except OSError as exc:
            self._lock.release()
            raise StorageError(f"cannot open database file {path}: {exc}") from exc
        size = os.fstat(self._fh.fileno()).st_size
        if size % PAGE_SIZE:
            log.warning("file size %d is not a multiple of the page size; ignoring torn tail", size)
        self._num_pages = size // PAGE_SIZE
        self.reads = 0
        self.writes = 0
        self.checkpoint_lsn = 0
        if self.created:
            self._write_header(0)
        else:
            self._read_header()

    # ------------------------------------------------------------------ header page
    def _write_header(self, checkpoint_lsn: int) -> None:
        page = Page(bytearray(PAGE_SIZE))
        page.format(PageType.HEADER, 0)
        FILE_HEADER.pack_into(page.data, COMMON_HEADER_SIZE, DB_MAGIC, DB_FORMAT_VERSION, PAGE_SIZE, checkpoint_lsn)
        self.write_page(HEADER_PAGE_ID, page.data)
        self.checkpoint_lsn = checkpoint_lsn

    def _read_header(self) -> None:
        data = self.read_page(HEADER_PAGE_ID)
        magic, version, page_size, ckpt = FILE_HEADER.unpack_from(data, COMMON_HEADER_SIZE)
        if magic != DB_MAGIC:
            raise StorageError(f"{self.path} is not a PyDB database (bad magic {magic!r})")
        if version != DB_FORMAT_VERSION:
            raise StorageError(f"unsupported file format version {version}")
        if page_size != PAGE_SIZE:
            raise StorageError(f"file uses page size {page_size}, engine uses {PAGE_SIZE}")
        self.checkpoint_lsn = ckpt

    def set_checkpoint_lsn(self, lsn: int) -> None:
        self._write_header(lsn)

    # ------------------------------------------------------------------ page I/O
    @property
    def num_pages(self) -> int:
        return self._num_pages

    def allocate_page_id(self) -> int:
        """Reserve a new page id at the end of the file.

        The bytes are written lazily (when the buffer pool flushes the page);
        reading an id past the current end of file returns a zero (FREE) page.
        """
        pid = self._num_pages
        self._num_pages += 1
        return pid

    def ensure_page_count(self, count: int) -> None:
        """Used by recovery: a WAL record may reference a page beyond EOF."""
        self._num_pages = max(self._num_pages, count)

    def read_page(self, page_id: int) -> bytearray:
        if page_id < 0:
            raise StorageError(f"invalid page id {page_id}")
        self._fh.seek(page_id * PAGE_SIZE)
        data = self._fh.read(PAGE_SIZE)
        self.reads += 1
        if len(data) < PAGE_SIZE:  # beyond EOF or torn tail: treat as never written
            return bytearray(PAGE_SIZE)
        if not verify_checksum(data):
            raise PageCorruptionError(f"checksum mismatch on page {page_id} of {self.path}")
        return bytearray(data)

    def write_page(self, page_id: int, data: bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise StorageError("write_page requires exactly one page of data")
        stamp_checksum(data)
        self._fh.seek(page_id * PAGE_SIZE)
        self._fh.write(data)
        self.writes += 1
        if page_id >= self._num_pages:
            self._num_pages = page_id + 1

    def sync(self) -> None:
        """Force written pages to stable storage."""
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def file_size(self) -> int:
        self._fh.flush()
        return os.fstat(self._fh.fileno()).st_size

    def close(self, sync: bool = True) -> None:
        if self._fh.closed:
            return
        if sync:
            self.sync()
        self._fh.close()
        self._lock.release()
