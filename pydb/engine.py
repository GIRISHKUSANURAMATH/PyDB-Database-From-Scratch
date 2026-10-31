"""The storage engine: wires every subsystem together and owns their lifecycle.

::

    Engine
     ├── DiskManager        database file, page I/O, checksums, file lock
     ├── WriteAheadLog      <db>.wal, LSNs, durable commits
     ├── BufferPool         LRU page cache, enforces the WAL rule on write-back
     ├── FreeSpaceManager   free page list + per-table free-space map
     ├── LockManager        table-level strict 2PL, deadlock detection
     ├── TransactionManager BEGIN/COMMIT/ROLLBACK, undo with CLRs
     ├── Catalog            system_tables / system_columns / system_indexes
     └── IndexManager       B+ trees

Startup sequence (``Engine.__init__``):

1. open the data file (new file → bootstrap system tables and checkpoint)
2. open the WAL; if it contains records the last shutdown was a crash →
   run ARIES-style recovery (analysis / redo / undo)
3. load the catalog cache from the system tables
4. scan page headers to rebuild free-space information and reclaim orphans
5. after a recovery: rebuild indexes of touched tables, then checkpoint
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from pydb.catalog.catalog import Catalog
from pydb.catalog.schema import Column, IndexInfo, TableSchema
from pydb.config import EngineConfig
from pydb.errors import (
    CatalogError,
    ColumnNotFoundError,
    ConstraintViolationError,
    ExecutionError,
    TransactionError,
)
from pydb.index.index_manager import IndexManager
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.disk import DiskManager
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.heap_file import apply_delete, apply_put
from pydb.storage.page import Page, PageType
from pydb.table import TableAccess
from pydb.transaction.lock_manager import LockManager
from pydb.transaction.recovery import RecoveryReport, recover
from pydb.transaction.transaction import Transaction, TransactionManager
from pydb.transaction.wal import LogRecord, LogType, WriteAheadLog

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, path: str, config: EngineConfig | None = None) -> None:
        self.path = os.path.abspath(path)
        self.config = config or EngineConfig()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        # Statement latch: one statement mutates shared structures at a time.
        # Transactional isolation comes from the lock manager, not from this latch.
        self.latch = threading.RLock()
        self.closed = False
        self.checkpoints = 0

        self.disk = DiskManager(self.path)
        try:
            self.wal = WriteAheadLog(self.path + ".wal", self.disk.checkpoint_lsn, sync=self.config.sync_commit)
        except Exception:
            self.disk.close(sync=False)
            raise
        self.pool = BufferPool(self.disk, self.config.buffer_pool_size)
        self.pool.wal_flush_to = self.wal.flush_to
        self.fsm = FreeSpaceManager(self.disk)
        self.locks = LockManager(self.config.lock_timeout)
        self.txm = TransactionManager(self.wal, self.locks, self.config.sync_commit)
        self.txm.undo_applier = self._undo
        self.catalog = Catalog(self.pool, self.fsm, self.txm)
        self.indexes = IndexManager(self.pool, self.fsm, self.config.btree_max_entries)
        self._tables: dict[int, TableAccess] = {}
        self.row_counts: dict[int, int] = {}
        self.last_recovery = RecoveryReport()
        try:
            self._startup()
        except BaseException:  # includes KeyboardInterrupt: never leave the file locked
            self._close_files(sync=False)
            raise

    # ================================================================== startup
    def _startup(self) -> None:
        if self.disk.created:
            log.info("creating new database %s", self.path)
            self.catalog.bootstrap()
            self.checkpoint()
        else:
            self.last_recovery = recover(self.wal, self.pool)
            self.txm.reseed(self.last_recovery.max_txn_id + 1)
        self.catalog.load()
        self._rebuild_free_space()
        if self.last_recovery.ran:
            self._rebuild_indexes_after_recovery()
            self.checkpoint()

    def _peek_page(self, pid: int) -> bytearray:
        """Read a page without polluting the cache (but see dirty cached copies)."""
        if pid in self.pool:
            with self.pool.pin(pid) as frame:
                return frame.data
        return self.disk.read_page(pid)

    def _wipe_page(self, pid: int) -> None:
        with self.pool.pin_new(pid) as frame:
            Page(frame.data).format(PageType.FREE, 0, self.wal.next_lsn - 1)

    def _rebuild_free_space(self) -> None:
        counts = self.fsm.rebuild(self.catalog.live_object_ids(), self._peek_page, self._wipe_page)
        self.row_counts = dict(self.fsm.row_counts)
        log.info("free-space scan: %s", counts)

    def _rebuild_indexes_after_recovery(self) -> None:
        touched = self.last_recovery.touched_objects
        catalog_touched = bool(touched & {s.id for s in self.catalog.system_schemas.values()})
        lsn = self.wal.next_lsn - 1
        suspects = []
        for info in self.catalog.indexes.values():
            table = self.catalog.table_by_id(info.table_id)
            if table is not None and (catalog_touched or table.id in touched):
                suspects.append((info, table))
                # The meta page may never have reached the disk (it would then look
                # FREE to the startup scan); claim it before allocating anything.
                self.fsm.reserve(info.meta_page)
        for info, table in suspects:
            log.info("rebuilding index %s after recovery", info.name)
            stale = [p for p in self.fsm.index_pages.get(info.id, []) if p != info.meta_page]
            self.free_pages(stale)
            self.indexes.forget(info.id)
            self.indexes.build(info.id, table, info.column, self.table(table).scan(), lsn, meta_page=info.meta_page)

    # ================================================================== accessors
    def table(self, schema: TableSchema) -> TableAccess:
        access = self._tables.get(schema.id)
        if access is None or access.schema is not schema:
            access = TableAccess(self, schema)
            self._tables[schema.id] = access
        return access

    def stats_row_delta(self, table_id: int, delta: int) -> None:
        self.row_counts[table_id] = self.row_counts.get(table_id, 0) + delta

    def row_count(self, table_id: int) -> int:
        return self.row_counts.get(table_id, 0)

    def child_has_value(self, child: TableSchema, column: str, value: Any) -> bool:
        return bool(self.table(child).index_lookup(column, value))

    def free_pages(self, page_ids) -> None:
        for pid in page_ids:
            self._wipe_page(pid)
            self.fsm.release_page(pid)

    # ================================================================== DDL
    def create_table(self, txn: Transaction, name: str, columns: list[Column]) -> TableSchema:
        name = name.lower()
        self._validate_columns(name, columns)
        txn.on_abort.append(self._reload_catalog)
        schema = self.catalog.add_table(txn, name, columns)
        self.row_counts[schema.id] = 0
        for col in columns:
            if col.primary_key:
                self.create_index(txn, name, f"pk_{name}", col.name, unique=True, auto=True)
            elif col.unique:
                self.create_index(txn, name, f"uq_{name}_{col.name}", col.name, unique=True, auto=True)
        return schema

    def _validate_columns(self, table: str, columns: list[Column]) -> None:
        if not columns:
            raise CatalogError("a table needs at least one column")
        names = [c.name for c in columns]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise CatalogError(f"duplicate column name(s): {', '.join(sorted(dupes))}")
        if sum(c.primary_key for c in columns) > 1:
            raise CatalogError("only one PRIMARY KEY column is supported")
        for c in columns:
            if c.has_default:
                from pydb.storage.record import coerce_value

                c.default = coerce_value(c.default, c.type, c.name)
            if c.references:
                ref_table, ref_col = c.references
                if ref_table == table:
                    raise CatalogError("self-referencing foreign keys are not supported")
                parent = self.catalog.get_table(ref_table)
                if not parent.has_column(ref_col):
                    raise ColumnNotFoundError(f"{ref_table} has no column {ref_col}")
                pcol = parent.column(ref_col)
                if not (pcol.primary_key or pcol.unique):
                    raise CatalogError(f"foreign key target {ref_table}.{ref_col} must be PRIMARY KEY or UNIQUE")
                if pcol.type is not c.type:
                    raise CatalogError(f"foreign key {table}.{c.name} type {c.type.value} != {pcol.type.value}")

    def drop_table(self, txn: Transaction, name: str) -> None:
        schema = self.catalog.get_table(name)
        if schema.system:
            raise ExecutionError("system tables cannot be dropped")
        for child in self.catalog.tables.values():
            if child.id != schema.id and any(c.references and c.references[0] == schema.name for c in child.columns):
                raise ConstraintViolationError(f"cannot drop {schema.name}: referenced by {child.name}")
        txn.on_abort.append(self._reload_catalog)
        heap_pages = self.table(schema).heap.page_ids()
        dropped_indexes = list(schema.indexes)
        index_pages = [p for ix in dropped_indexes for p in self.indexes.tree(ix).all_page_ids()]
        self.catalog.remove_table(txn, schema.name)

        def release() -> None:  # only once the DROP is durable
            self.free_pages(heap_pages + index_pages)
            self.fsm.forget_owner(schema.id)
            for ix in dropped_indexes:
                self.indexes.forget(ix.id)
            self._tables.pop(schema.id, None)
            self.row_counts.pop(schema.id, None)

        txn.on_commit.append(release)

    def create_index(
        self, txn: Transaction, table_name: str, name: str, column: str, unique: bool = False, auto: bool = False
    ) -> IndexInfo:
        schema = self.catalog.get_table(table_name)
        if schema.system:
            raise ExecutionError("cannot index system tables")
        name = name.lower()
        if name in self.catalog.indexes:
            from pydb.errors import IndexAlreadyExistsError

            raise IndexAlreadyExistsError(f"index {name} already exists")
        col = schema.column(column)
        rows = list(self.table(schema).scan())
        if unique:
            pos = schema.position(col.name)
            seen: set[Any] = set()
            for _rid, row in rows:
                v = row[pos]
                if v is not None and v in seen:
                    raise ConstraintViolationError(f"cannot create unique index {name}: duplicate value {v!r}")
                seen.add(v)
        txn.on_abort.append(self._reload_catalog)
        meta_page = self.fsm.allocate_page()
        info = IndexInfo(self.catalog.next_object_id(), name, schema.id, col.name, meta_page, unique, auto)
        self.catalog.add_index(txn, info)  # logged: makes the index durable at COMMIT
        tree = self.indexes.build(info.id, schema, col.name, rows, txn.last_lsn, meta_page=meta_page)
        txn.on_abort.append(lambda: self.free_pages(tree.all_page_ids()))
        return info

    def drop_index(self, txn: Transaction, name: str) -> None:
        info = self.catalog.get_index(name)
        if info.auto:
            raise ExecutionError(f"index {info.name} enforces a PRIMARY KEY/UNIQUE constraint and cannot be dropped")
        pages = self.indexes.tree(info).all_page_ids()
        txn.on_abort.append(self._reload_catalog)
        self.catalog.remove_index(txn, info.name)

        def release() -> None:
            self.free_pages(pages)
            self.indexes.forget(info.id)

        txn.on_commit.append(release)

    def _reload_catalog(self) -> None:
        self.catalog.load()
        self.indexes.clear_cache()
        self._tables.clear()

    # ================================================================== undo (rollback)
    def _undo(self, txn: Transaction, rec: LogRecord) -> None:
        """Reverse one change during ROLLBACK, logging a CLR, and fix the indexes."""
        if rec.type is LogType.NEW_PAGE:
            return
        schema = self.catalog.table_by_id(rec.owner)
        decode = schema.serializer.decode if schema is not None else None
        if rec.type is LogType.INSERT:
            clr = LogRecord(LogType.DELETE, txn.txn_id, owner=rec.owner, page_id=rec.page_id, slot=rec.slot,
                            before=rec.after)
            lsn = self.txm.log_clr(txn, clr)
            apply_delete(self.pool, rec.owner, rec.rid, lsn)
            if schema is not None and not schema.system:
                self.indexes.delete_row(schema, rec.rid, decode(rec.after), lsn)
                self.stats_row_delta(schema.id, -1)
        elif rec.type is LogType.DELETE:
            clr = LogRecord(LogType.INSERT, txn.txn_id, owner=rec.owner, page_id=rec.page_id, slot=rec.slot,
                            after=rec.before)
            lsn = self.txm.log_clr(txn, clr)
            apply_put(self.pool, rec.owner, rec.rid, rec.before, lsn)
            if schema is not None and not schema.system:
                self.indexes.insert_row(schema, rec.rid, decode(rec.before), lsn)
                self.stats_row_delta(schema.id, +1)
        elif rec.type is LogType.UPDATE:
            clr = LogRecord(LogType.UPDATE, txn.txn_id, owner=rec.owner, page_id=rec.page_id, slot=rec.slot,
                            before=rec.after, after=rec.before)
            lsn = self.txm.log_clr(txn, clr)
            apply_put(self.pool, rec.owner, rec.rid, rec.before, lsn)
            if schema is not None and not schema.system:
                self.indexes.update_row(schema, rec.rid, decode(rec.after), rec.rid, decode(rec.before), lsn)
        self._refresh_free(rec.owner, rec.page_id)

    def _refresh_free(self, owner: int, page_id: int) -> None:
        from pydb.storage.page import HeapPage

        with self.pool.pin(page_id) as frame:
            self.fsm.record_free(owner, page_id, HeapPage(frame.data).free_space_for_insert())

    # ================================================================== checkpoint / shutdown
    def checkpoint(self) -> None:
        """Sharp checkpoint: make the data file self-sufficient, then empty the WAL.

        Only legal when no transaction has uncommitted changes, because their
        undo information lives in the log we are about to truncate.
        """
        with self.latch:
            if any(t.has_writes for t in self.txm.active_transactions()):
                raise TransactionError("cannot checkpoint while transactions with changes are active")
            self.wal.flush()
            self.pool.flush_all()
            self.disk.sync()
            self.disk.set_checkpoint_lsn(self.wal.next_lsn)
            self.disk.sync()
            self.wal.truncate()
            self.checkpoints += 1

    def maybe_checkpoint(self) -> None:
        if self.wal.size() >= self.config.checkpoint_wal_bytes and not any(
            t.has_writes for t in self.txm.active_transactions()
        ):
            self.checkpoint()

    def close(self) -> None:
        if self.closed:
            return
        with self.latch:
            for txn in self.txm.active_transactions():
                log.warning("rolling back unfinished transaction T%d at close", txn.txn_id)
                self.txm.rollback(txn)
            self.checkpoint()
            self._close_files(sync=True)

    def simulate_crash(self) -> None:
        """Stop *now*: lose the buffer pool and un-flushed WAL, like a killed process."""
        if self.closed:
            return
        self.wal.discard_buffer()
        self.pool.discard_all()
        self._close_files(sync=False)

    def _close_files(self, sync: bool) -> None:
        self.closed = True
        try:
            if sync:
                self.wal.close()
            else:
                self.wal._fh.close()
        finally:
            self.disk.close(sync=sync)

    # ================================================================== introspection
    def stats(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "pages": self.disk.num_pages,
            "free_pages": self.fsm.free_page_count,
            "tables": len(self.catalog.tables),
            "indexes": len(self.catalog.indexes),
            "buffer_pool": {"capacity": self.pool.capacity, "resident": len(self.pool),
                            "dirty": len(self.pool.dirty_page_ids()), **self.pool.stats.as_dict()},
            "wal": {"bytes": self.wal.size(), "next_lsn": self.wal.next_lsn, "flushes": self.wal.flush_count},
            "transactions": {"active": len(self.txm.active_transactions()), "commits": self.txm.commits,
                             "aborts": self.txm.aborts},
            "locks": {"waits": self.locks.waits, "deadlocks": self.locks.deadlocks},
            "disk": {"reads": self.disk.reads, "writes": self.disk.writes},
            "checkpoints": self.checkpoints,
        }
