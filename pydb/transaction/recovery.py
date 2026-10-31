"""Crash recovery from the write-ahead log (a simplified ARIES).

Runs at startup whenever the WAL is not empty — a clean shutdown always ends
with a checkpoint that empties the log, so a non-empty log means the previous
process did not shut down cleanly.

Three passes:

1. **Analysis** — scan the log, classify each transaction as *committed*,
   *aborted* (rolled back, its undo already logged as CLRs) or a *loser*
   (neither COMMIT nor ABORT: it was in flight when the crash happened). Also
   collect the set of objects (tables) whose pages were touched.

2. **Redo** — "repeat history": re-apply *every* change in LSN order, for all
   transactions including losers and CLRs, but only to pages whose
   ``page_lsn`` is older than the record. After redo the buffer pool holds
   exactly the state of the moment of the crash, independent of which pages
   happened to have been flushed.

3. **Undo** — roll back the losers by applying the before-images of their
   (non-CLR) records in reverse LSN order. Strict 2PL guarantees no other
   transaction touched those rows, and every undo step is "set slot to
   before-image", so repeating undo after a crash *during* recovery is safe.

Afterwards the engine rebuilds the indexes of every touched table from the
recovered heap (index pages are not WAL-logged — see docs/recovery.md) and
takes a checkpoint, which empties the log.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pydb.storage.buffer_pool import BufferPool
from pydb.storage.heap_file import apply_delete, apply_new_page, apply_put
from pydb.storage.page import Page
from pydb.transaction.wal import LogRecord, LogType, WriteAheadLog

log = logging.getLogger(__name__)

DATA_TYPES = (LogType.INSERT, LogType.DELETE, LogType.UPDATE, LogType.NEW_PAGE)


@dataclass
class RecoveryReport:
    records: int = 0
    committed: set[int] = field(default_factory=set)
    aborted: set[int] = field(default_factory=set)
    losers: set[int] = field(default_factory=set)
    redone: int = 0
    skipped: int = 0
    undone: int = 0
    touched_objects: set[int] = field(default_factory=set)
    max_txn_id: int = 0

    @property
    def ran(self) -> bool:
        return self.records > 0

    def summary(self) -> str:
        return (
            f"recovery: {self.records} log records, {len(self.committed)} committed, "
            f"{len(self.aborted)} aborted, {len(self.losers)} rolled back; "
            f"redo applied {self.redone} (skipped {self.skipped} already on disk), undo applied {self.undone}"
        )


def recover(wal: WriteAheadLog, pool: BufferPool) -> RecoveryReport:
    report = RecoveryReport()
    records = wal.records()
    report.records = len(records)
    if not records:
        return report

    # ---- 1. analysis
    seen: set[int] = set()
    for rec in records:
        seen.add(rec.txn_id)
        report.max_txn_id = max(report.max_txn_id, rec.txn_id)
        if rec.type is LogType.COMMIT:
            report.committed.add(rec.txn_id)
        elif rec.type is LogType.ABORT:
            report.aborted.add(rec.txn_id)
        elif rec.type in DATA_TYPES:
            report.touched_objects.add(rec.owner)
    report.losers = seen - report.committed - report.aborted
    log.info("analysis: committed=%s aborted=%s losers=%s",
             sorted(report.committed), sorted(report.aborted), sorted(report.losers))

    # ---- 2. redo (repeat history)
    for rec in records:
        if rec.type not in DATA_TYPES:
            continue
        if _redo(pool, rec):
            report.redone += 1
        else:
            report.skipped += 1

    # ---- 3. undo losers, newest change first
    for rec in reversed(records):
        if rec.txn_id in report.losers and rec.type in DATA_TYPES and not rec.is_clr:
            _undo(pool, rec)
            report.undone += 1

    log.info(report.summary())
    return report


def _page_lsn(pool: BufferPool, page_id: int) -> int:
    with pool.pin(page_id) as frame:
        return Page(frame.data).lsn


def _redo(pool: BufferPool, rec: LogRecord) -> bool:
    if rec.type is LogType.NEW_PAGE:
        apply_new_page(pool, rec, rec.lsn, redo=True)
        return True
    pool.disk.ensure_page_count(rec.page_id + 1)
    if _page_lsn(pool, rec.page_id) >= rec.lsn:
        return False  # this change already reached the disk before the crash
    if rec.type is LogType.DELETE:
        apply_delete(pool, rec.owner, rec.rid, rec.lsn)
    else:  # INSERT or UPDATE: the after-image is the slot's new content
        apply_put(pool, rec.owner, rec.rid, rec.after, rec.lsn)
    return True


def _undo(pool: BufferPool, rec: LogRecord) -> None:
    # Undo does not raise the page LSN: re-running redo then undo after a second
    # crash yields the same result because both are "set slot to image".
    if rec.type is LogType.NEW_PAGE:
        return  # an empty linked page is harmless; it is reused by later inserts
    if rec.type is LogType.INSERT:
        apply_delete(pool, rec.owner, rec.rid, 0)
    else:  # DELETE or UPDATE: restore the before-image
        apply_put(pool, rec.owner, rec.rid, rec.before, 0)
