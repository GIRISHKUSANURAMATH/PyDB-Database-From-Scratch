"""Heap file: an unordered collection of records stored in a chain of slotted pages.

A table's rows live in a singly linked list of heap pages::

    first_page --> page --> page --> ... --> last_page --> INVALID

New rows go to any page with enough free space (found through the free-space
map), otherwise a new page is allocated and linked after the last one.

Every modification follows the write-ahead protocol:

1. build a WAL record describing the change (with before/after images)
2. append it to the log  -> obtain its LSN
3. apply the change to the page in the buffer pool
4. stamp the page with that LSN and mark it dirty

The page cannot be written to disk until the log is flushed past its LSN
(enforced by the buffer pool), so the log always "knows" more than the disk.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydb.config import INVALID_PAGE_ID
from pydb.errors import RecordTooLargeError, StorageError
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.page import MAX_RECORD_SIZE, HeapPage, PageType
from pydb.transaction.transaction import Transaction, TransactionManager
from pydb.transaction.wal import LogRecord, LogType

RID = tuple[int, int]  # (page_id, slot)


class HeapFile:
    def __init__(
        self,
        pool: BufferPool,
        fsm: FreeSpaceManager,
        txm: TransactionManager,
        owner_id: int,
        first_page_id: int,
    ) -> None:
        self.pool = pool
        self.fsm = fsm
        self.txm = txm
        self.owner_id = owner_id
        self.first_page_id = first_page_id

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(
        cls, pool: BufferPool, fsm: FreeSpaceManager, txm: TransactionManager, owner_id: int, txn: Transaction
    ) -> HeapFile:
        heap = cls(pool, fsm, txm, owner_id, INVALID_PAGE_ID)
        heap.first_page_id = heap._new_page(txn, prev_page=INVALID_PAGE_ID)
        return heap

    def _new_page(self, txn: Transaction, prev_page: int) -> int:
        pid = self.fsm.allocate_page()
        rec = LogRecord(LogType.NEW_PAGE, txn.txn_id, owner=self.owner_id, page_id=pid, prev_page=prev_page)
        lsn = self.txm.log(txn, rec)
        apply_new_page(self.pool, rec, lsn)
        self.fsm.set_last_page(self.owner_id, pid)
        with self.pool.pin(pid) as frame:
            self.fsm.record_free(self.owner_id, pid, HeapPage(frame.data).free_space_for_insert())
        return pid

    # ------------------------------------------------------------------ reads
    def get(self, rid: RID) -> bytes:
        page_id, slot = rid
        with self.pool.pin(page_id) as frame:
            page = HeapPage(frame.data)
            if page.page_type is not PageType.HEAP or page.owner_id != self.owner_id:
                raise StorageError(f"RID {rid} does not belong to object {self.owner_id}")
            return page.get(slot)

    def scan(self) -> Iterator[tuple[RID, bytes]]:
        """Sequential scan: follow the page chain, yielding every live record."""
        pid = self.first_page_id
        while pid != INVALID_PAGE_ID:
            with self.pool.pin(pid) as frame:
                page = HeapPage(frame.data)
                rows = list(page.records())  # copy out so the page is unpinned quickly
                nxt = page.next_page
            for slot, data in rows:
                yield (pid, slot), data
            pid = nxt

    def page_ids(self) -> list[int]:
        pids, pid = [], self.first_page_id
        while pid != INVALID_PAGE_ID:
            pids.append(pid)
            with self.pool.pin(pid) as frame:
                pid = HeapPage(frame.data).next_page
        return pids

    # ------------------------------------------------------------------ writes
    def insert(self, txn: Transaction, record: bytes) -> RID:
        if len(record) > MAX_RECORD_SIZE:
            raise RecordTooLargeError(f"record of {len(record)} bytes exceeds {MAX_RECORD_SIZE}")
        pid = self.fsm.find_page(self.owner_id, len(record))
        if pid is None:
            pid = self._new_page(txn, prev_page=self._last_page())
        with self.pool.pin(pid) as frame:
            page = HeapPage(frame.data)
            if page.free_space_for_insert() < len(record):  # FSM was stale
                self.fsm.record_free(self.owner_id, pid, page.free_space_for_insert())
                pid = None
            else:
                slot = page.find_free_slot()
                rec = LogRecord(LogType.INSERT, txn.txn_id, owner=self.owner_id, page_id=pid, slot=slot, after=record)
                rec.lsn = self.txm.log(txn, rec)
                page.put(slot, record)
                page.lsn = rec.lsn
                frame.dirty = True
                self.fsm.record_free(self.owner_id, pid, page.free_space_for_insert())
                return (pid, slot)
        return self.insert(txn, record)  # retry with a corrected FSM

    def delete(self, txn: Transaction, rid: RID) -> bytes:
        page_id, slot = rid
        with self.pool.pin(page_id) as frame:
            page = HeapPage(frame.data)
            old = page.get(slot)
            rec = LogRecord(LogType.DELETE, txn.txn_id, owner=self.owner_id, page_id=page_id, slot=slot, before=old)
            page.lsn = self.txm.log(txn, rec)
            page.delete(slot)
            frame.dirty = True
            self.fsm.record_free(self.owner_id, page_id, page.free_space_for_insert())
            return old

    def update(self, txn: Transaction, rid: RID, record: bytes) -> RID:
        """Replace a record. Returns the (possibly new) RID.

        If the new version still fits in its page it is updated in place and
        keeps its RID; otherwise it is moved (delete + insert) and the caller
        must update indexes with the new RID.
        """
        page_id, slot = rid
        with self.pool.pin(page_id) as frame:
            page = HeapPage(frame.data)
            old = page.get(slot)
            if len(record) <= page.total_free() + len(old):  # fits in place (after compaction)
                rec = LogRecord(
                    LogType.UPDATE, txn.txn_id, owner=self.owner_id, page_id=page_id, slot=slot,
                    before=old, after=record,
                )
                lsn = self.txm.log(txn, rec)
                page.put(slot, record)
                page.lsn = lsn
                frame.dirty = True
                self.fsm.record_free(self.owner_id, page_id, page.free_space_for_insert())
                return rid
        self.delete(txn, rid)
        return self.insert(txn, record)

    def _last_page(self) -> int:
        last = self.fsm.last_page(self.owner_id)
        if last is None:  # not cached (should not happen after startup) — walk the chain
            last = self.page_ids()[-1]
            self.fsm.set_last_page(self.owner_id, last)
        return last


# ---------------------------------------------------------------------- physical apply
# These functions are shared by normal execution, rollback and crash recovery.
# Each is idempotent with respect to the page LSN.


def apply_new_page(pool: BufferPool, rec: LogRecord, lsn: int, redo: bool = False) -> None:
    """Format a new heap page and link it after ``rec.prev_page``.

    With ``redo=True`` each half is applied only if that page has not already
    seen this LSN (the page LSN test that makes redo idempotent).
    """
    pool.disk.ensure_page_count(rec.page_id + 1)
    if redo:
        with pool.pin(rec.page_id) as frame:
            if HeapPage(frame.data).lsn < lsn:
                HeapPage(frame.data).init(rec.owner, lsn)
                frame.dirty = True
    else:
        with pool.pin_new(rec.page_id) as frame:
            HeapPage(frame.data).init(rec.owner, lsn)
    if rec.prev_page != INVALID_PAGE_ID:
        with pool.pin(rec.prev_page) as frame:
            prev = HeapPage(frame.data)
            if prev.lsn < lsn:
                if prev.page_type is not PageType.HEAP:
                    prev.init(rec.owner)
                prev.next_page = rec.page_id
                prev.lsn = lsn
                frame.dirty = True


def apply_put(pool: BufferPool, owner: int, rid: RID, data: bytes, lsn: int) -> None:
    page_id, slot = rid
    with pool.pin(page_id) as frame:
        page = HeapPage(frame.data)
        if page.page_type is not PageType.HEAP:
            page.init(owner)
        page.put(slot, data)
        page.lsn = max(page.lsn, lsn)
        frame.dirty = True


def apply_delete(pool: BufferPool, owner: int, rid: RID, lsn: int) -> None:
    page_id, slot = rid
    with pool.pin(page_id) as frame:
        page = HeapPage(frame.data)
        if page.page_type is not PageType.HEAP:
            page.init(owner)
        page.delete(slot)
        page.lsn = max(page.lsn, lsn)
        frame.dirty = True
