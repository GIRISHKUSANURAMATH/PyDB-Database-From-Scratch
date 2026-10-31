"""Free-space management.

Two questions the storage engine must answer quickly:

1. *Which page can I reuse when I need a brand-new page?*  → the **free page list**
2. *Which page of table T has room for a 120-byte row?*   → the **free space map**

PyDB keeps both in memory and rebuilds them at startup with a single pass over
the page headers. This works because every page is self-describing: its
header records its type and its owner object (see ``page.py``). A page is
considered free if it is typed FREE *or* if its owner no longer exists in the
catalog (e.g. a table was dropped and the process crashed before the pages
were wiped). Such orphan pages are wiped during the scan.

Trade-off (documented in docs/storage.md): startup cost is O(file size). A
production engine would persist the map (PostgreSQL's FSM fork) instead.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Iterable

from pydb.config import INVALID_PAGE_ID
from pydb.storage.disk import DiskManager
from pydb.storage.page import HeapPage, PageType, read_common_header

log = logging.getLogger(__name__)

# Pages with less free space than this are not worth remembering as insert targets.
MIN_USEFUL_FREE = 32


class FreeSpaceManager:
    def __init__(self, disk: DiskManager) -> None:
        self.disk = disk
        self._free_pages: list[int] = []  # sorted, so allocation is deterministic
        self._heap_free: dict[int, dict[int, int]] = {}  # owner -> {page_id: free bytes}
        self._last_page: dict[int, int] = {}  # owner -> last page of the heap chain
        # Filled by rebuild(): pages of each index, and live-row counts per table.
        self.index_pages: dict[int, list[int]] = {}
        self.row_counts: dict[int, int] = {}

    # ------------------------------------------------------------------ page allocation
    def allocate_page(self) -> int:
        if self._free_pages:
            return self._free_pages.pop(0)
        return self.disk.allocate_page_id()

    def release_page(self, page_id: int) -> None:
        if page_id not in self._free_pages:
            bisect.insort(self._free_pages, page_id)

    def reserve(self, page_id: int) -> None:
        """Take a specific page off the free list (it is about to be (re)used)."""
        if page_id in self._free_pages:
            self._free_pages.remove(page_id)
        # It may lie beyond the current end of file (allocated but never flushed):
        # make sure the allocator will not hand out the same id again.
        self.disk.ensure_page_count(page_id + 1)

    @property
    def free_page_count(self) -> int:
        return len(self._free_pages)

    def free_pages(self) -> list[int]:
        return list(self._free_pages)

    # ------------------------------------------------------------------ heap free-space map
    def record_free(self, owner: int, page_id: int, free: int) -> None:
        pages = self._heap_free.setdefault(owner, {})
        if free >= MIN_USEFUL_FREE:
            pages[page_id] = free
        else:
            pages.pop(page_id, None)

    def find_page(self, owner: int, needed: int) -> int | None:
        pages = self._heap_free.get(owner)
        if not pages:
            return None
        last = self._last_page.get(owner)
        if last is not None and pages.get(last, 0) >= needed:
            return last
        for pid, free in pages.items():
            if free >= needed:
                return pid
        return None

    def last_page(self, owner: int) -> int | None:
        return self._last_page.get(owner)

    def set_last_page(self, owner: int, page_id: int) -> None:
        self._last_page[owner] = page_id

    def forget_owner(self, owner: int) -> None:
        self._heap_free.pop(owner, None)
        self._last_page.pop(owner, None)

    # ------------------------------------------------------------------ startup scan
    def rebuild(self, live_owners: Iterable[int], read_page=None, wipe_page=None) -> dict[str, int]:
        """Scan every page header and rebuild the free list and free-space map.

        ``read_page(pid)`` returns page bytes (through the buffer pool, so pages
        dirtied by recovery are seen); ``wipe_page(pid)`` formats an orphan page as
        FREE. Returns counts for logging/inspection.
        """
        read_page = read_page or self.disk.read_page
        live = set(live_owners)
        self._free_pages.clear()
        self._heap_free.clear()
        self._last_page.clear()
        self.index_pages.clear()
        self.row_counts.clear()
        counts = {"free": 0, "orphans": 0, "heap": 0, "index": 0}
        for pid in range(1, self.disk.num_pages):
            data = read_page(pid)
            _lsn, ptype, owner, nxt = read_common_header(data)
            if ptype is PageType.FREE:
                self._free_pages.append(pid)
                counts["free"] += 1
                continue
            if owner not in live:
                log.info("page %d belongs to dropped object %d; reclaiming", pid, owner)
                if wipe_page is not None:
                    wipe_page(pid)
                self._free_pages.append(pid)
                counts["orphans"] += 1
                continue
            if ptype is PageType.HEAP:
                counts["heap"] += 1
                heap = HeapPage(data)
                self.record_free(owner, pid, heap.free_space_for_insert())
                self.row_counts[owner] = self.row_counts.get(owner, 0) + sum(1 for _ in heap.records())
                if nxt == INVALID_PAGE_ID:
                    self._last_page[owner] = pid
            else:
                counts["index"] += 1
                self.index_pages.setdefault(owner, []).append(pid)
        return counts
