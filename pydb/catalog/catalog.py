"""The system catalog: metadata about tables, columns and indexes.

The catalog is stored *in the database itself*, in three heap tables with
fixed first pages (1, 2 and 3), so it uses exactly the same storage, WAL and
recovery code paths as user data::

    system_tables  (id, name, first_page)
    system_columns (table_id, position, name, type, not_null, is_unique,
                    primary_key, default_value, ref_table, ref_column)
    system_indexes (id, name, table_id, column_name, meta_page, is_unique, is_auto)

Because catalog changes are ordinary logged heap operations, a crash during
``CREATE TABLE`` is recovered like any other transaction. The in-memory
dictionaries below are just a cache, reloaded from these tables at startup.

The system tables are read-only to SQL users but can be queried, e.g.
``SELECT * FROM system_tables``.
"""

from __future__ import annotations

import json
from typing import Any

from pydb.catalog.schema import Column, IndexInfo, TableSchema
from pydb.config import (
    FIRST_USER_OBJECT_ID,
    SYSTEM_COLUMNS_ID,
    SYSTEM_COLUMNS_PAGE_ID,
    SYSTEM_INDEXES_ID,
    SYSTEM_INDEXES_PAGE_ID,
    SYSTEM_TABLES_ID,
    SYSTEM_TABLES_PAGE_ID,
)
from pydb.errors import (
    IndexAlreadyExistsError,
    IndexNotFoundError,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.heap_file import HeapFile
from pydb.storage.page import HeapPage
from pydb.storage.record import ColumnType
from pydb.transaction.transaction import Transaction, TransactionManager

I, T, B = ColumnType.INTEGER, ColumnType.TEXT, ColumnType.BOOLEAN

SYSTEM_SCHEMAS: list[TableSchema] = [
    TableSchema(SYSTEM_TABLES_ID, "system_tables", SYSTEM_TABLES_PAGE_ID,
                [Column("id", I), Column("name", T), Column("first_page", I)], system=True),
    TableSchema(SYSTEM_COLUMNS_ID, "system_columns", SYSTEM_COLUMNS_PAGE_ID,
                [Column("table_id", I), Column("position", I), Column("name", T), Column("type", T),
                 Column("not_null", B), Column("is_unique", B), Column("primary_key", B),
                 Column("default_value", T), Column("ref_table", T), Column("ref_column", T)], system=True),
    TableSchema(SYSTEM_INDEXES_ID, "system_indexes", SYSTEM_INDEXES_PAGE_ID,
                [Column("id", I), Column("name", T), Column("table_id", I), Column("column_name", T),
                 Column("meta_page", I), Column("is_unique", B), Column("is_auto", B)], system=True),
]


class Catalog:
    def __init__(self, pool: BufferPool, fsm: FreeSpaceManager, txm: TransactionManager) -> None:
        self.pool = pool
        self.fsm = fsm
        self.txm = txm
        self._system = {s.name: s for s in SYSTEM_SCHEMAS}
        self._heaps = {s.id: HeapFile(pool, fsm, txm, s.id, s.first_page) for s in SYSTEM_SCHEMAS}
        self.tables: dict[str, TableSchema] = {}
        self.indexes: dict[str, IndexInfo] = {}

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self) -> None:
        """Format the three system-table pages of a brand-new database file."""
        for schema in SYSTEM_SCHEMAS:
            with self.pool.pin_new(schema.first_page) as frame:
                HeapPage(frame.data).init(schema.id)
            self.pool.disk.ensure_page_count(schema.first_page + 1)
            self.fsm.set_last_page(schema.id, schema.first_page)

    def load(self) -> None:
        """(Re)build the in-memory cache from the system tables."""
        tables: dict[int, TableSchema] = {}
        cols: dict[int, list[tuple[int, Column]]] = {}
        for _rid, row in self._rows(SYSTEM_COLUMNS_ID):
            tid, pos, name, typ, nn, uq, pk, dflt, rtab, rcol = row
            has_default = dflt is not None
            col = Column(
                name, ColumnType(typ), bool(nn), bool(uq), bool(pk), has_default,
                json.loads(dflt) if has_default else None,
                (rtab, rcol) if rtab else None,
            )
            cols.setdefault(tid, []).append((pos, col))
        for _rid, (tid, name, first_page) in self._rows(SYSTEM_TABLES_ID):
            columns = [c for _, c in sorted(cols.get(tid, []), key=lambda pc: pc[0])]
            tables[tid] = TableSchema(tid, name, first_page, columns)
        self.indexes = {}
        for _rid, row in self._rows(SYSTEM_INDEXES_ID):
            ix = IndexInfo(row[0], row[1], row[2], row[3], row[4], bool(row[5]), bool(row[6]))
            self.indexes[ix.name] = ix
            if ix.table_id in tables:
                tables[ix.table_id].indexes.append(ix)
        self.tables = {t.name: t for t in tables.values()}

    def _rows(self, system_id: int):
        schema = next(s for s in SYSTEM_SCHEMAS if s.id == system_id)
        for rid, data in self._heaps[system_id].scan():
            yield rid, schema.serializer.decode(data)

    # ------------------------------------------------------------------ lookup
    def get_table(self, name: str) -> TableSchema:
        name = name.lower()
        if name in self._system:
            return self._system[name]
        try:
            return self.tables[name]
        except KeyError:
            raise TableNotFoundError(f"no such table: {name}") from None

    def table_by_id(self, table_id: int) -> TableSchema | None:
        for s in SYSTEM_SCHEMAS:
            if s.id == table_id:
                return s
        return next((t for t in self.tables.values() if t.id == table_id), None)

    def has_table(self, name: str) -> bool:
        return name.lower() in self.tables or name.lower() in self._system

    def get_index(self, name: str) -> IndexInfo:
        try:
            return self.indexes[name.lower()]
        except KeyError:
            raise IndexNotFoundError(f"no such index: {name}") from None

    def live_object_ids(self) -> set[int]:
        ids = {s.id for s in SYSTEM_SCHEMAS}
        ids.update(t.id for t in self.tables.values())
        ids.update(ix.id for ix in self.indexes.values())
        return ids

    def next_object_id(self) -> int:
        return max([FIRST_USER_OBJECT_ID - 1, *self.live_object_ids()]) + 1

    def system_rows(self, name: str) -> list[tuple[Any, ...]]:
        """Rows of a system table, for ``SELECT * FROM system_tables`` etc."""
        schema = self._system[name]
        return [row for _rid, row in self._rows(schema.id)]

    @property
    def system_schemas(self) -> dict[str, TableSchema]:
        return self._system

    # ------------------------------------------------------------------ DDL (logged)
    def add_table(self, txn: Transaction, name: str, columns: list[Column]) -> TableSchema:
        name = name.lower()
        if self.has_table(name):
            raise TableAlreadyExistsError(f"table {name} already exists")
        table_id = self.next_object_id()
        heap = HeapFile.create(self.pool, self.fsm, self.txm, table_id, txn)
        schema = TableSchema(table_id, name, heap.first_page_id, columns)
        self._insert(txn, SYSTEM_TABLES_ID, (table_id, name, heap.first_page_id))
        for pos, c in enumerate(columns):
            dflt = json.dumps(c.default) if c.has_default else None
            ref_t, ref_c = c.references if c.references else (None, None)
            self._insert(txn, SYSTEM_COLUMNS_ID,
                         (table_id, pos, c.name, c.type.value, c.not_null, c.unique, c.primary_key, dflt, ref_t, ref_c))
        self.tables[name] = schema
        return schema

    def remove_table(self, txn: Transaction, name: str) -> TableSchema:
        schema = self.get_table(name)
        for ix in list(schema.indexes):
            self.remove_index(txn, ix.name)
        self._delete_where(txn, SYSTEM_TABLES_ID, lambda row: row[0] == schema.id)
        self._delete_where(txn, SYSTEM_COLUMNS_ID, lambda row: row[0] == schema.id)
        del self.tables[schema.name]
        return schema

    def add_index(self, txn: Transaction, info: IndexInfo) -> None:
        if info.name in self.indexes:
            raise IndexAlreadyExistsError(f"index {info.name} already exists")
        self._insert(txn, SYSTEM_INDEXES_ID,
                     (info.id, info.name, info.table_id, info.column, info.meta_page, info.unique, info.auto))
        self.indexes[info.name] = info
        table = self.table_by_id(info.table_id)
        if table is not None:
            table.indexes.append(info)

    def remove_index(self, txn: Transaction, name: str) -> IndexInfo:
        info = self.get_index(name)
        self._delete_where(txn, SYSTEM_INDEXES_ID, lambda row: row[0] == info.id)
        del self.indexes[info.name]
        table = self.table_by_id(info.table_id)
        if table is not None:
            table.indexes = [ix for ix in table.indexes if ix.id != info.id]
        return info

    def _insert(self, txn: Transaction, system_id: int, row: tuple) -> None:
        schema = next(s for s in SYSTEM_SCHEMAS if s.id == system_id)
        self._heaps[system_id].insert(txn, schema.serializer.encode(row))

    def _delete_where(self, txn: Transaction, system_id: int, pred) -> None:
        for rid, row in list(self._rows(system_id)):
            if pred(row):
                self._heaps[system_id].delete(txn, rid)
