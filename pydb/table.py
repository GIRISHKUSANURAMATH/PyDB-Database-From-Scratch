"""Row-level table operations: heap + indexes + constraints in one place.

This is the layer the query executor calls for INSERT/UPDATE/DELETE. Each
method validates constraints, writes the heap (which logs to the WAL), and
then maintains every index of the table.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from pydb.catalog.schema import TableSchema
from pydb.constraints import constraints as cons
from pydb.errors import ExecutionError
from pydb.storage.heap_file import RID, HeapFile
from pydb.transaction.transaction import Transaction

if TYPE_CHECKING:
    from pydb.engine import Engine


class TableAccess:
    def __init__(self, engine: Engine, schema: TableSchema) -> None:
        self.engine = engine
        self.schema = schema
        self.heap = HeapFile(engine.pool, engine.fsm, engine.txm, schema.id, schema.first_page)

    # ------------------------------------------------------------------ reads
    def scan(self) -> Iterator[tuple[RID, tuple]]:
        decode = self.schema.serializer.decode
        for rid, data in self.heap.scan():
            yield rid, decode(data)

    def fetch(self, rid: RID) -> tuple:
        return self.schema.serializer.decode(self.heap.get(rid))

    def index_lookup(self, column: str, value: Any) -> list[tuple[RID, tuple]]:
        info = self.schema.index_on(column)
        if info is None:
            return [(rid, row) for rid, row in self.scan() if row[self.schema.position(column)] == value]
        return [(rid, self.fetch(rid)) for rid in self.engine.indexes.lookup(info, value)]

    # ------------------------------------------------------------------ writes
    def _check_writable(self) -> None:
        if self.schema.system:
            raise ExecutionError(f"{self.schema.name} is a read-only system table")

    def insert(self, txn: Transaction, row: tuple) -> RID:
        self._check_writable()
        t = self.schema
        cons.check_not_null(t, row)
        cons.check_unique(self.engine.indexes, t, row)
        cons.check_foreign_keys(self.engine.catalog, self.engine.indexes, t, row)
        rid = self.heap.insert(txn, t.serializer.encode(row))
        self.engine.indexes.insert_row(t, rid, row, txn.last_lsn)
        self.engine.stats_row_delta(t.id, +1)
        return rid

    def delete(self, txn: Transaction, rid: RID, row: tuple) -> None:
        self._check_writable()
        cons.check_not_referenced(self.engine.catalog, self.schema, row, None, self.engine.child_has_value)
        self.heap.delete(txn, rid)
        self.engine.indexes.delete_row(self.schema, rid, row, txn.last_lsn)
        self.engine.stats_row_delta(self.schema.id, -1)

    def update(self, txn: Transaction, rid: RID, old: tuple, new: tuple) -> RID:
        self._check_writable()
        t = self.schema
        cons.check_not_null(t, new)
        cons.check_unique(self.engine.indexes, t, new, ignore_rid=rid)
        cons.check_foreign_keys(self.engine.catalog, self.engine.indexes, t, new)
        cons.check_not_referenced(self.engine.catalog, t, old, new, self.engine.child_has_value)
        new_rid = self.heap.update(txn, rid, t.serializer.encode(new))
        self.engine.indexes.update_row(t, rid, old, new_rid, new, txn.last_lsn)
        return new_rid
