"""Base class for physical query operators (the *iterator* / Volcano model).

Every operator:

* exposes ``columns`` — the :class:`Binding` list describing its output tuples,
* is iterable — iterating pulls rows from its children on demand,
* can describe itself for ``EXPLAIN``.

Operators are composed into a tree by the planner; the root is iterated by the
executor. Pull-based iteration means a ``LIMIT 10`` on top of a scan stops the
scan after ten rows instead of reading the whole table.
"""

from __future__ import annotations

from collections.abc import Iterator

from pydb.sql.expressions import Binding


class Operator:
    columns: list[Binding]
    children: list[Operator]

    def __init__(self, columns: list[Binding], children: list[Operator] | None = None) -> None:
        self.columns = columns
        self.children = children or []
        self.rows_produced = 0

    def __iter__(self) -> Iterator[tuple]:
        for row in self.produce():
            self.rows_produced += 1
            yield row

    def produce(self) -> Iterator[tuple]:  # pragma: no cover - abstract
        raise NotImplementedError

    def label(self) -> str:  # pragma: no cover - abstract
        return type(self).__name__

    def explain_lines(self, depth: int = 0) -> list[str]:
        prefix = "" if depth == 0 else "  " * (depth - 1) + "-> "
        lines = [prefix + self.label()]
        for child in self.children:
            lines.extend(child.explain_lines(depth + 1))
        return lines

    def explain(self) -> str:
        return "\n".join(self.explain_lines())
