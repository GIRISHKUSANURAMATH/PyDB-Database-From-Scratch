"""ORDER BY, LIMIT/OFFSET and DISTINCT."""

from __future__ import annotations

from collections.abc import Iterator
from itertools import islice

from pydb.execution.operators import Operator
from pydb.sql.expressions import Evaluator


def _null_first_key(v: object) -> tuple:
    # NULLs sort before every value (SQLite convention); ints and floats compare
    # together; mixed incomparable types fall back to grouping by type name.
    if v is None:
        return (0, 0, 0)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (1, 0, v)
    return (1, 1, type(v).__name__, v) if not isinstance(v, str) else (1, 2, v)


class Sort(Operator):
    """In-memory sort. Multi-key ORDER BY uses successive *stable* sorts,
    from the last key to the first, each ascending or descending."""

    def __init__(self, child: Operator, keys: list[tuple[Evaluator, bool]], description: str) -> None:
        super().__init__(child.columns, [child])
        self.keys = keys
        self.description = description

    def produce(self) -> Iterator[tuple]:
        rows = list(self.children[0])
        for key_fn, descending in reversed(self.keys):
            rows.sort(key=lambda r, f=key_fn: _null_first_key(f(r)), reverse=descending)
        yield from rows

    def label(self) -> str:
        return f"Sort({self.description})"


class Limit(Operator):
    def __init__(self, child: Operator, limit: int | None, offset: int = 0) -> None:
        super().__init__(child.columns, [child])
        self.limit = limit
        self.offset = offset

    def produce(self) -> Iterator[tuple]:
        stop = None if self.limit is None else self.offset + self.limit
        yield from islice(self.children[0], self.offset, stop)

    def label(self) -> str:
        off = f" offset {self.offset}" if self.offset else ""
        return f"Limit({self.limit}{off})"


class Distinct(Operator):
    def __init__(self, child: Operator) -> None:
        super().__init__(child.columns, [child])

    def produce(self) -> Iterator[tuple]:
        seen: set[tuple] = set()
        for row in self.children[0]:
            if row not in seen:
                seen.add(row)
                yield row

    def label(self) -> str:
        return "Distinct"
