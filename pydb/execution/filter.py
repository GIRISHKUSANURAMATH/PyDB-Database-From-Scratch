"""Filter operator: keeps rows whose predicate evaluates to exactly TRUE."""

from __future__ import annotations

from collections.abc import Iterator

from pydb.execution.operators import Operator
from pydb.sql.expressions import Evaluator


class Filter(Operator):
    def __init__(self, child: Operator, predicate: Evaluator, description: str) -> None:
        super().__init__(child.columns, [child])
        self.predicate = predicate
        self.description = description

    def produce(self) -> Iterator[tuple]:
        pred = self.predicate
        for row in self.children[0]:
            if pred(row) is True:  # NULL (unknown) is filtered out, like FALSE
                yield row

    def label(self) -> str:
        return f"Filter({self.description})"
