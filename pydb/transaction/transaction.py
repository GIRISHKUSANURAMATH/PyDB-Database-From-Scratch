"""Transactions and the transaction manager.

A :class:`Transaction` collects the WAL records it wrote (its *undo list*) and
the locks it holds. The :class:`TransactionManager` implements the protocol:

* ``BEGIN`` is logged lazily — read-only transactions never touch the log.
* ``COMMIT``: append a COMMIT record, **fsync the WAL** (this is the moment the
  transaction becomes durable), then release locks (strict 2PL).
* ``ROLLBACK``: walk the undo list backwards and undo each change, logging a
  compensation record (CLR) for every undo, then append ABORT.
* Savepoints: each statement records the length of the undo list; if the
  statement fails, only its own changes are undone (statement atomicity).
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from pydb.errors import TransactionError
from pydb.transaction.lock_manager import LockManager
from pydb.transaction.wal import FLAG_CLR, LogRecord, LogType, WriteAheadLog

log = logging.getLogger(__name__)


class TxnState(Enum):
    ACTIVE = "active"
    COMMITTED = "committed"
    ABORTED = "aborted"


@dataclass(eq=False)
class Transaction:
    txn_id: int
    explicit: bool = False  # started with BEGIN (vs. autocommit)
    state: TxnState = TxnState.ACTIVE
    undo: list[LogRecord] = field(default_factory=list)
    begin_logged: bool = False
    last_lsn: int = 0
    # Deferred actions, e.g. freeing the pages of a dropped table only once the
    # DROP is durable, or reloading the catalog cache after a failed DDL.
    on_commit: list[Callable[[], None]] = field(default_factory=list)
    on_abort: list[Callable[[], None]] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.state is TxnState.ACTIVE

    @property
    def has_writes(self) -> bool:
        return self.begin_logged

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Transaction T{self.txn_id} {self.state.value} undo={len(self.undo)}>"


# An "undo applier" reverses one logged change (heap + indexes) and returns the
# CLR describing the compensation, already applied to the page.
UndoApplier = Callable[[Transaction, LogRecord], None]


class TransactionManager:
    def __init__(self, wal: WriteAheadLog, locks: LockManager, sync_commit: bool = True) -> None:
        self.wal = wal
        self.locks = locks
        self.sync_commit = sync_commit
        self._ids = itertools.count(1)
        self._active: dict[int, Transaction] = {}
        self._mutex = threading.Lock()
        self.undo_applier: UndoApplier | None = None  # installed by the engine
        self.commits = 0
        self.aborts = 0

    def reseed(self, next_id: int) -> None:
        self._ids = itertools.count(next_id)

    # ------------------------------------------------------------------ lifecycle
    def begin(self, explicit: bool = False) -> Transaction:
        with self._mutex:
            txn = Transaction(next(self._ids), explicit=explicit)
            self._active[txn.txn_id] = txn
        return txn

    def active_transactions(self) -> list[Transaction]:
        with self._mutex:
            return list(self._active.values())

    def log(self, txn: Transaction, rec: LogRecord) -> int:
        """Append a data-change record on behalf of ``txn`` and return its LSN."""
        if not txn.is_active:
            raise TransactionError(f"transaction T{txn.txn_id} is not active")
        if not txn.begin_logged:
            txn.last_lsn = self.wal.append(LogRecord(LogType.BEGIN, txn.txn_id))
            txn.begin_logged = True
        rec.txn_id = txn.txn_id
        lsn = self.wal.append(rec)
        txn.last_lsn = lsn
        if not rec.is_clr:
            txn.undo.append(rec)
        return lsn

    def log_clr(self, txn: Transaction, rec: LogRecord) -> int:
        rec.flags |= FLAG_CLR
        return self.log(txn, rec)

    def commit(self, txn: Transaction) -> None:
        self._require_active(txn)
        if txn.begin_logged:
            self.wal.append(LogRecord(LogType.COMMIT, txn.txn_id))
            if self.sync_commit:
                self.wal.flush()  # <- durability point
        txn.state = TxnState.COMMITTED
        for action in txn.on_commit:
            action()
        self._finish(txn)
        self.commits += 1

    def rollback(self, txn: Transaction) -> None:
        self._require_active(txn)
        self.rollback_to(txn, 0)
        if txn.begin_logged:
            self.wal.append(LogRecord(LogType.ABORT, txn.txn_id))
        txn.state = TxnState.ABORTED
        for action in txn.on_abort:
            action()
        self._finish(txn)
        self.aborts += 1

    def rollback_to(self, txn: Transaction, savepoint: int) -> None:
        """Undo every change made after ``savepoint`` (an undo-list length)."""
        if self.undo_applier is None:  # pragma: no cover - wiring error
            raise TransactionError("no undo applier installed")
        while len(txn.undo) > savepoint:
            rec = txn.undo.pop()
            self.undo_applier(txn, rec)

    def _finish(self, txn: Transaction) -> None:
        with self._mutex:
            self._active.pop(txn.txn_id, None)
        self.locks.release_all(txn.txn_id)

    @staticmethod
    def _require_active(txn: Transaction) -> None:
        if not txn.is_active:
            raise TransactionError(f"transaction T{txn.txn_id} already {txn.state.value}")
