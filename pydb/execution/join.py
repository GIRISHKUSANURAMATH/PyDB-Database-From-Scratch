"""Join operators.

* :class:`NestedLoopJoin` — for every left row, scan the (materialized) right
  input and test the condition. O(n*m) but works for any condition.
* :class:`HashJoin` — for equi-joins (``a.x = b.y``): build a hash table on the
  right input's key, then probe it with each left row. O(n+m).
* :class:`IndexNestedLoopJoin` — for equi-joins where the right table has an
  index on the join column: probe the B+ tree once per left row.

All three support INNER and LEFT joins. For LEFT joins, a left row with no
match is emitted once, padded with NULLs for the right side.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydb.catalog.schema import IndexInfo
from pydb.execution.operators import Operator
from pydb.index.index_manager import IndexManager
from pydb.sql.expressions import Evaluator
from pydb.table import TableAccess


class NestedLoopJoin(Operator):
    def __init__(self, left: Operator, right: Operator, condition: Evaluator | None, kind: str, desc: str) -> None:
        super().__init__(left.columns + right.columns, [left, right])
        self.condition = condition
        self.kind = kind
        self.desc = desc

    def produce(self) -> Iterator[tuple]:
        right_rows = list(self.children[1])  # materialize the inner side once
        pad = (None,) * len(self.children[1].columns)
        cond = self.condition
        for lrow in self.children[0]:
            matched = False
            for rrow in right_rows:
                row = lrow + rrow
                if cond is None or cond(row) is True:
                    matched = True
                    yield row
            if not matched and self.kind == "LEFT":
                yield lrow + pad

    def label(self) -> str:
        on = f" ON {self.desc}" if self.desc else ""
        return f"NestedLoopJoin({self.kind}{on})"


class HashJoin(Operator):
    def __init__(
        self, left: Operator, right: Operator, left_key: Evaluator, right_key: Evaluator,
        residual: Evaluator | None, kind: str, desc: str,
    ) -> None:
        super().__init__(left.columns + right.columns, [left, right])
        self.left_key, self.right_key = left_key, right_key
        self.residual = residual
        self.kind = kind
        self.desc = desc

    def produce(self) -> Iterator[tuple]:
        table: dict[object, list[tuple]] = {}
        for rrow in self.children[1]:  # build phase
            k = self.right_key(rrow)
            if k is not None:  # NULL never equals anything
                table.setdefault(_hash_key(k), []).append(rrow)
        pad = (None,) * len(self.children[1].columns)
        for lrow in self.children[0]:  # probe phase
            k = self.left_key(lrow)
            matched = False
            if k is not None:
                for rrow in table.get(_hash_key(k), ()):
                    row = lrow + rrow
                    if self.residual is None or self.residual(row) is True:
                        matched = True
                        yield row
            if not matched and self.kind == "LEFT":
                yield lrow + pad

    def label(self) -> str:
        return f"HashJoin({self.kind} ON {self.desc})"


def _hash_key(k: object) -> object:
    # 1 and 1.0 must land in the same bucket (they compare equal in SQL).
    if isinstance(k, float) and k.is_integer():
        return int(k)
    return k


class IndexNestedLoopJoin(Operator):
    def __init__(
        self, left: Operator, right_table: TableAccess, right_alias_columns: list, index: IndexInfo,
        indexes: IndexManager, left_key: Evaluator, residual: Evaluator | None, kind: str, desc: str,
    ) -> None:
        super().__init__(left.columns + right_alias_columns, [left])
        self.right_table = right_table
        self.right_width = len(right_alias_columns)
        self.index = index
        self.indexes = indexes
        self.left_key = left_key
        self.residual = residual
        self.kind = kind
        self.desc = desc

    def produce(self) -> Iterator[tuple]:
        tree = self.indexes.tree(self.index)
        pad = (None,) * self.right_width
        for lrow in self.children[0]:
            k = self.left_key(lrow)
            matched = False
            if k is not None:
                for rid in tree.search(k):
                    row = lrow + self.right_table.fetch(rid)
                    if self.residual is None or self.residual(row) is True:
                        matched = True
                        yield row
            if not matched and self.kind == "LEFT":
                yield lrow + pad

    def label(self) -> str:
        return f"IndexNestedLoopJoin({self.kind} {self.right_table.schema.name} via {self.index.name} ON {self.desc})"
