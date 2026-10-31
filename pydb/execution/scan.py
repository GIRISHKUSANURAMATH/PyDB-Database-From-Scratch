"""Access-path operators: where rows come from.

* :class:`SeqScan` reads every page of the table's heap file in order.
* :class:`IndexScan` walks a B+ tree for a key or key range and fetches each
  matching row by its RID — far fewer pages when the predicate is selective.
* :class:`SystemTableScan` / :class:`SingleRow` serve catalog tables and
  ``SELECT`` without ``FROM``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydb.catalog.schema import IndexInfo
from pydb.execution.operators import Operator
from pydb.index.index_manager import IndexManager
from pydb.sql.expressions import Binding
from pydb.table import TableAccess


def table_bindings(alias: str, names: list[str]) -> list[Binding]:
    return [Binding(alias, n) for n in names]


class SeqScan(Operator):
    def __init__(self, table: TableAccess, alias: str, estimated_rows: int = 0) -> None:
        super().__init__(table_bindings(alias, table.schema.column_names))
        self.table = table
        self.alias = alias
        self.estimated_rows = estimated_rows

    def rid_rows(self) -> Iterator[tuple[tuple[int, int], tuple]]:
        return self.table.scan()

    def produce(self) -> Iterator[tuple]:
        for _rid, row in self.table.scan():
            yield row

    def label(self) -> str:
        alias = f" AS {self.alias}" if self.alias != self.table.schema.name else ""
        return f"SeqScan({self.table.schema.name}{alias})  rows~{self.estimated_rows}"


class IndexScan(Operator):
    """Point lookup (``low == high``) or range scan over a B+ tree."""

    def __init__(
        self,
        table: TableAccess,
        alias: str,
        index: IndexInfo,
        indexes: IndexManager,
        low: Any = None,
        high: Any = None,
        low_inclusive: bool = True,
        high_inclusive: bool = True,
        condition: str = "",
    ) -> None:
        super().__init__(table_bindings(alias, table.schema.column_names))
        self.table = table
        self.alias = alias
        self.index = index
        self.indexes = indexes
        self.low, self.high = low, high
        self.low_inclusive, self.high_inclusive = low_inclusive, high_inclusive
        self.condition = condition

    def rid_rows(self) -> Iterator[tuple[tuple[int, int], tuple]]:
        tree = self.indexes.tree(self.index)
        rids = [(e[1], e[2]) for e in tree.range_entries(self.low, self.high, self.low_inclusive, self.high_inclusive)]
        for rid in rids:  # materialize RIDs first: DML may modify the tree while we iterate
            yield rid, self.table.fetch(rid)

    def produce(self) -> Iterator[tuple]:
        for _rid, row in self.rid_rows():
            yield row

    def label(self) -> str:
        return f"IndexScan({self.table.schema.name}, {self.index.name}: {self.condition})"


class SystemTableScan(Operator):
    def __init__(self, name: str, alias: str, column_names: list[str], rows: list[tuple]) -> None:
        super().__init__(table_bindings(alias, column_names))
        self.name = name
        self._rows = rows

    def produce(self) -> Iterator[tuple]:
        yield from self._rows

    def label(self) -> str:
        return f"SystemTableScan({self.name})"


class SingleRow(Operator):
    """The one empty row that ``SELECT 1 + 1`` (no FROM clause) is evaluated against."""

    def __init__(self) -> None:
        super().__init__([])

    def produce(self) -> Iterator[tuple]:
        yield ()

    def label(self) -> str:
        return "SingleRow"
