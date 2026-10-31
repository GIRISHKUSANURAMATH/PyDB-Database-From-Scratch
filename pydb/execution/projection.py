"""Projection: computes the SELECT list (choose / rename / calculate columns)."""

from __future__ import annotations

from collections.abc import Iterator

from pydb.execution.operators import Operator
from pydb.sql.expressions import Binding, Evaluator


class Projection(Operator):
    def __init__(self, child: Operator, evaluators: list[Evaluator], names: list[str]) -> None:
        super().__init__([Binding(None, n) for n in names], [child])
        self.evaluators = evaluators
        self.names = names

    def produce(self) -> Iterator[tuple]:
        evals = self.evaluators
        for row in self.children[0]:
            yield tuple(e(row) for e in evals)

    def label(self) -> str:
        return f"Projection({', '.join(self.names)})"
