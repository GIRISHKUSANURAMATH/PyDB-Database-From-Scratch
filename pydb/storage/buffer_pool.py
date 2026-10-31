"""Buffer pool: an in-memory cache of disk pages with LRU replacement.

Why a buffer pool? Disk I/O is orders of magnitude slower than memory. Every
component above the storage layer (heap files, B+ trees, the catalog) asks the
buffer pool for a page instead of reading the file; the pool serves it from
memory when it can (a *hit*) and reads it from disk otherwise (a *miss*).

Key ideas implemented here:

* **Frames** — fixed number of page-sized slots (``capacity``).
* **Pinning** — a page in use by an operation is *pinned* and can never be
  evicted. ``with pool.pin(page_id) as frame:`` pins/unpins automatically.
* **Dirty pages** — a modified page is marked dirty and written back only
  when evicted or flushed (*write-back* caching).
* **LRU eviction** — when the pool is full, the least-recently-used unpinned
  page is evicted.
* **WAL rule** — before a dirty page is written, the write-ahead log is flushed
  up to that page's LSN. This guarantees the log always describes every change
  that has reached the data file, which is what makes crash recovery possible.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from pydb.config import PAGE_SIZE
from pydb.errors import BufferPoolFullError
from pydb.storage.disk import DiskManager
from pydb.storage.page import Page

log = logging.getLogger(__name__)


@dataclass
class Frame:
    page_id: int
    data: bytearray
    pin_count: int = 0
    dirty: bool = False

    @property
    def lsn(self) -> int:
        return Page(self.data).lsn


@dataclass
class BufferPoolStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    dirty_flushes: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_ratio": round(self.hit_ratio, 4),
            "evictions": self.evictions,
            "dirty_flushes": self.dirty_flushes,
        }


class BufferPool:
    def __init__(self, disk: DiskManager, capacity: int = 256) -> None:
        if capacity < 4:
            raise ValueError("buffer pool needs at least 4 frames")
        self.disk = disk
        self.capacity = capacity
        # page_id -> Frame, ordered from least to most recently used.
        self._frames: OrderedDict[int, Frame] = OrderedDict()
        self.stats = BufferPoolStats()
        # Installed by the engine: flush the WAL up to (at least) the given LSN.
        self.wal_flush_to: Callable[[int], None] = lambda lsn: None

    # ------------------------------------------------------------------ access
    @contextmanager
    def pin(self, page_id: int) -> Iterator[Frame]:
        """Fetch ``page_id`` (from cache or disk) and keep it pinned for the block."""
        frame = self._fetch(page_id)
        frame.pin_count += 1
        try:
            yield frame
        finally:
            frame.pin_count -= 1

    @contextmanager
    def pin_new(self, page_id: int) -> Iterator[Frame]:
        """Create a frame for a freshly allocated page without reading disk."""
        frame = self._frames.get(page_id)
        if frame is None:
            self._make_room()
            frame = Frame(page_id, bytearray(PAGE_SIZE))
            self._frames[page_id] = frame
        else:
            frame.data[:] = bytes(PAGE_SIZE)
            self._frames.move_to_end(page_id)
        frame.dirty = True
        frame.pin_count += 1
        try:
            yield frame
        finally:
            frame.pin_count -= 1

    def _fetch(self, page_id: int) -> Frame:
        frame = self._frames.get(page_id)
        if frame is not None:
            self.stats.hits += 1
            self._frames.move_to_end(page_id)
            return frame
        self.stats.misses += 1
        self._make_room()
        frame = Frame(page_id, self.disk.read_page(page_id))
        self._frames[page_id] = frame
        return frame

    def _make_room(self) -> None:
        if len(self._frames) < self.capacity:
            return
        for page_id, frame in self._frames.items():  # iterates LRU -> MRU
            if frame.pin_count == 0:
                self._write_back(frame)
                del self._frames[page_id]
                self.stats.evictions += 1
                log.debug("evicted page %d", page_id)
                return
        raise BufferPoolFullError(f"all {self.capacity} buffer frames are pinned")

    # ------------------------------------------------------------------ write-back
    def _write_back(self, frame: Frame) -> None:
        if not frame.dirty:
            return
        self.wal_flush_to(frame.lsn)  # Write-Ahead Logging rule
        self.disk.write_page(frame.page_id, frame.data)
        frame.dirty = False
        self.stats.dirty_flushes += 1

    def flush_page(self, page_id: int) -> None:
        frame = self._frames.get(page_id)
        if frame is not None:
            self._write_back(frame)

    def flush_all(self) -> None:
        for frame in list(self._frames.values()):
            self._write_back(frame)

    def discard_all(self) -> None:
        """Drop every cached page *without* writing it (used to simulate a crash)."""
        self._frames.clear()

    # ------------------------------------------------------------------ introspection
    def dirty_page_ids(self) -> list[int]:
        return [pid for pid, f in self._frames.items() if f.dirty]

    def resident_page_ids(self) -> list[int]:
        return list(self._frames)

    def __contains__(self, page_id: int) -> bool:
        return page_id in self._frames

    def __len__(self) -> int:
        return len(self._frames)
