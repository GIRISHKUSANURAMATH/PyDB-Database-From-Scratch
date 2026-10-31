"""Index manager: owns the B+ tree objects and keeps them in sync with tables.

Every index indexes one column. NULLs are not indexed (so ``UNIQUE`` allows
many NULLs, as in standard SQL), and ``IS NULL`` predicates use a sequential
scan.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from pydb.catalog.schema import IndexInfo, TableSchema
from pydb.index.bplus_tree import BPlusTree
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.heap_file import RID

log = logging.getLogger(__name__)


class IndexManager:
    def __init__(self, pool: BufferPool, fsm: FreeSpaceManager, max_entries: int | None = None) -> None:
        self.pool = pool
        self.fsm = fsm
        self.max_entries = max_entries
        self._trees: dict[int, BPlusTree] = {}

    def tree(self, info: IndexInfo) -> BPlusTree:
        tree = self._trees.get(info.id)
        if tree is None:
            tree = BPlusTree(self.pool, self.fsm, info.meta_page, info.id, self.max_entries)
            self._trees[info.id] = tree
        return tree

    def forget(self, index_id: int) -> None:
        self._trees.pop(index_id, None)

    def clear_cache(self) -> None:
        self._trees.clear()

    # ------------------------------------------------------------------ build
    def build(
        self, index_id: int, table: TableSchema, column: str, rows: Iterable[tuple[RID, tuple]],
        lsn: int, meta_page: int | None = None,
    ) -> BPlusTree:
        """Create a new tree and insert ``(value, rid)`` for every existing row."""
        col = table.column(column)
        pos = table.position(column)
        tree = BPlusTree.create(self.pool, self.fsm, index_id, col.type, lsn, self.max_entries, meta_page)
        for rid, row in rows:
            if row[pos] is not None:
                tree.insert(row[pos], rid, lsn)
        self._trees[index_id] = tree
        return tree

    # ------------------------------------------------------------------ row maintenance
    def insert_row(self, table: TableSchema, rid: RID, row: tuple, lsn: int) -> None:
        for info in table.indexes:
            value = row[table.position(info.column)]
            if value is not None:
                self.tree(info).insert(value, rid, lsn)

    def delete_row(self, table: TableSchema, rid: RID, row: tuple, lsn: int) -> None:
        for info in table.indexes:
            value = row[table.position(info.column)]
            if value is not None:
                self.tree(info).delete(value, rid, lsn)

    def update_row(self, table: TableSchema, old_rid: RID, old: tuple, new_rid: RID, new: tuple, lsn: int) -> None:
        for info in table.indexes:
            pos = table.position(info.column)
            if old_rid == new_rid and old[pos] == new[pos] and type(old[pos]) is type(new[pos]):
                continue  # key unchanged and row did not move: nothing to do
            tree = self.tree(info)
            if old[pos] is not None:
                tree.delete(old[pos], old_rid, lsn)
            if new[pos] is not None:
                tree.insert(new[pos], new_rid, lsn)

    def lookup(self, info: IndexInfo, value: Any) -> list[RID]:
        return self.tree(info).search(value)
