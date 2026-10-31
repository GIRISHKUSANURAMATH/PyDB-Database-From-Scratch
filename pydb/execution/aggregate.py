"""Hash aggregation: GROUP BY + COUNT / SUM / AVG / MIN / MAX.

Rows are grouped in a dictionary keyed by the GROUP BY values; each group
keeps one accumulator per aggregate. Output tuples are
``(group values..., aggregate results...)``. With no GROUP BY there is exactly
one group — even for an empty input, so ``SELECT COUNT(*) FROM empty`` is 0.

NULL handling follows SQL: aggregates ignore NULL inputs; ``COUNT(*)`` counts
rows; SUM/AVG/MIN/MAX of no non-NULL values is NULL.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydb.errors import ExecutionError
from pydb.execution.operators import Operator
from pydb.sql.expressions import Binding, Evaluator


@dataclass
class AggSpec:
    func: str  # COUNT SUM AVG MIN MAX
    arg: Evaluator | None  # None for COUNT(*)
    distinct: bool
    label: str


class _Acc:
    __slots__ = ("spec", "count", "total", "best", "seen")

    def __init__(self, spec: AggSpec) -> None:
        self.spec = spec
        self.count = 0
        self.total: Any = 0
        self.best: Any = None
        self.seen: set[Any] | None = set() if spec.distinct else None

    def add(self, row: tuple) -> None:
        spec = self.spec
        if spec.arg is None:  # COUNT(*)
            self.count += 1
            return
        v = spec.arg(row)
        if v is None:
            return
        if self.seen is not None:
            if v in self.seen:
                return
            self.seen.add(v)
        self.count += 1
        f = spec.func
        if f in ("SUM", "AVG"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ExecutionError(f"{f}() needs numeric values, got {v!r}")
            self.total += v
        elif f == "MIN":
            if self.best is None or v < self.best:
                self.best = v
        elif f == "MAX":
            if self.best is None or v > self.best:
                self.best = v

    def result(self) -> Any:
        f = self.spec.func
        if f == "COUNT":
            return self.count
        if f == "SUM":
            return self.total if self.count else None
        if f == "AVG":
            return self.total / self.count if self.count else None
        return self.best


class HashAggregate(Operator):
    def __init__(
        self, child: Operator, group_evals: list[Evaluator], group_labels: list[str], aggs: list[AggSpec]
    ) -> None:
        cols = [Binding(None, lbl) for lbl in group_labels] + [Binding(None, a.label) for a in aggs]
        super().__init__(cols, [child])
        self.group_evals = group_evals
        self.group_labels = group_labels
        self.aggs = aggs

    def produce(self) -> Iterator[tuple]:
        groups: dict[tuple, list[_Acc]] = {}
        for row in self.children[0]:
            key = tuple(g(row) for g in self.group_evals)
            accs = groups.get(key)
            if accs is None:
                accs = groups[key] = [_Acc(a) for a in self.aggs]
            for acc in accs:
                acc.add(row)
        if not groups and not self.group_evals:
            groups[()] = [_Acc(a) for a in self.aggs]  # aggregate over zero rows
        for key, accs in groups.items():
            yield key + tuple(acc.result() for acc in accs)

    def label(self) -> str:
        parts = []
        if self.group_labels:
            parts.append("group by " + ", ".join(self.group_labels))
        if self.aggs:
            parts.append(", ".join(a.label for a in self.aggs))
        return f"HashAggregate({'; '.join(parts)})"
