"""Expression evaluation, independent of the parser.

An AST expression is *compiled* once per query into a tree of small Python
closures. Column references are resolved to tuple positions at compile time
(the "binding" step), so evaluating a row is just ``fn(row)`` — no name
lookups, no string parsing and, importantly, no ``eval()``.

SQL uses **three-valued logic**: comparisons involving NULL yield NULL
("unknown", represented by ``None``), ``NULL AND FALSE`` is FALSE,
``NULL OR TRUE`` is TRUE, and a WHERE clause keeps a row only when its
condition is exactly TRUE.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydb.errors import ColumnNotFoundError, ExecutionError
from pydb.sql import ast

Row = tuple
Evaluator = Callable[[Row], Any]


@dataclass(frozen=True)
class Binding:
    """One column of an operator's output: which table (alias) and name it has."""

    table: str | None
    name: str

    def __str__(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Slot(ast.Expr):
    """Internal node: "value at position ``index`` of the input row".

    The planner rewrites aggregate calls and GROUP BY expressions into slots
    that point at the output columns of the Aggregate operator.
    """

    index: int
    label: str


def resolve(ref: ast.ColumnRef, scope: Sequence[Binding]) -> int:
    matches = [i for i, b in enumerate(scope) if b.name == ref.name and (ref.table is None or b.table == ref.table)]
    if not matches:
        raise ColumnNotFoundError(f"no such column: {ref}")
    if len(matches) > 1:
        tables = ", ".join(str(scope[i]) for i in matches)
        raise ColumnNotFoundError(f"ambiguous column {ref}: could be {tables}")
    return matches[0]


def is_true(value: Any) -> bool:
    return value is True


# ============================================================================ compiler
def compile_expr(expr: ast.Expr, scope: Sequence[Binding], params: Sequence[Any] = ()) -> Evaluator:
    if isinstance(expr, ast.Literal):
        value = expr.value
        return lambda row: value
    if isinstance(expr, ast.Param):
        if expr.index >= len(params):
            raise ExecutionError(f"statement needs at least {expr.index + 1} parameter(s), got {len(params)}")
        value = params[expr.index]
        return lambda row: value
    if isinstance(expr, ast.ColumnRef):
        idx = resolve(expr, scope)
        return lambda row: row[idx]
    if isinstance(expr, Slot):
        idx = expr.index
        return lambda row: row[idx]
    if isinstance(expr, ast.UnaryOp):
        operand = compile_expr(expr.operand, scope, params)
        if expr.op == "NOT":
            return lambda row: _not(operand(row))
        if expr.op == "-":
            return lambda row: _negate(operand(row))
        return operand
    if isinstance(expr, ast.BinaryOp):
        return _compile_binary(expr, scope, params)
    if isinstance(expr, ast.IsNull):
        operand = compile_expr(expr.operand, scope, params)
        if expr.negated:
            return lambda row: operand(row) is not None
        return lambda row: operand(row) is None
    if isinstance(expr, ast.InList):
        return _compile_in(expr, scope, params)
    if isinstance(expr, ast.Between):
        x = compile_expr(expr.operand, scope, params)
        lo = compile_expr(expr.low, scope, params)
        hi = compile_expr(expr.high, scope, params)
        neg = expr.negated

        def between(row: Row) -> Any:
            v = x(row)
            result = _and(_compare(">=", v, lo(row)), _compare("<=", v, hi(row)))
            return _not(result) if neg else result

        return between
    if isinstance(expr, ast.FuncCall):
        if expr.name in ast.AGGREGATES:
            raise ExecutionError(f"aggregate {expr.name}() is not allowed here")
        return _compile_scalar_function(expr, scope, params)
    if isinstance(expr, ast.Star):
        raise ExecutionError("* is only allowed in the SELECT list or COUNT(*)")
    raise ExecutionError(f"cannot evaluate {expr!r}")  # pragma: no cover


def evaluate_constant(expr: ast.Expr, params: Sequence[Any] = ()) -> Any:
    """Evaluate an expression that references no columns (e.g. VALUES, LIMIT)."""
    return compile_expr(expr, (), params)(())


# ============================================================================ operators
def _not(v: Any) -> Any:
    return None if v is None else not v


def _negate(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ExecutionError(f"cannot negate {v!r}")
    return -v


def _and(a: Any, b: Any) -> Any:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True


def _or(a: Any, b: Any) -> Any:
    if a is True or b is True:
        return True
    if a is None or b is None:
        return None
    return False


def _comparable(a: Any, b: Any) -> bool:
    num = (int, float)
    if isinstance(a, num) and isinstance(b, num):
        return True
    return type(a) is type(b)


def _compare(op: str, a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    if not _comparable(a, b):
        raise ExecutionError(f"cannot compare {type(a).__name__} {a!r} with {type(b).__name__} {b!r}")
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    return a >= b


def _arith(op: str, a: Any, b: Any) -> Any:
    if a is None or b is None:
        return None
    for v in (a, b):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ExecutionError(f"arithmetic operator {op} needs numbers, got {v!r}")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        return None  # division by zero yields NULL (as in SQLite)
    if op == "/":
        if isinstance(a, int) and isinstance(b, int):
            q = abs(a) // abs(b)  # SQL integer division truncates toward zero
            return q if (a >= 0) == (b >= 0) else -q
        return a / b
    if isinstance(a, int) and isinstance(b, int):  # % takes the sign of the dividend
        return int(math.fmod(a, b))
    return math.fmod(a, b)


def like_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a SQL LIKE pattern (% = any run, _ = one char) to a regex."""
    out = []
    for ch in pattern:
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def _compile_binary(expr: ast.BinaryOp, scope, params) -> Evaluator:
    left = compile_expr(expr.left, scope, params)
    right = compile_expr(expr.right, scope, params)
    op = expr.op
    if op == "AND":
        return lambda row: _and(left(row), right(row))
    if op == "OR":
        return lambda row: _or(left(row), right(row))
    if op in ("=", "!=", "<", "<=", ">", ">="):
        return lambda row: _compare(op, left(row), right(row))
    if op in ("+", "-", "*", "/", "%"):
        return lambda row: _arith(op, left(row), right(row))
    if op == "LIKE":
        cache: dict[str, re.Pattern[str]] = {}

        def like(row: Row) -> Any:
            value, pattern = left(row), right(row)
            if value is None or pattern is None:
                return None
            if not isinstance(value, str) or not isinstance(pattern, str):
                raise ExecutionError("LIKE needs TEXT operands")
            rx = cache.get(pattern)
            if rx is None:
                rx = cache[pattern] = like_to_regex(pattern)
            return rx.fullmatch(value) is not None

        return like
    raise ExecutionError(f"unknown operator {op}")  # pragma: no cover


def _compile_in(expr: ast.InList, scope, params) -> Evaluator:
    x = compile_expr(expr.operand, scope, params)
    items = [compile_expr(i, scope, params) for i in expr.items]
    neg = expr.negated

    def in_list(row: Row) -> Any:
        v = x(row)
        if v is None:
            return None
        saw_null = False
        for item in items:
            w = item(row)
            if w is None:
                saw_null = True
            elif _compare("=", v, w):
                return not neg
        if saw_null:
            return None
        return neg

    return in_list


_SCALAR_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "UPPER": lambda s: None if s is None else str(s).upper(),
    "LOWER": lambda s: None if s is None else str(s).lower(),
    "LENGTH": lambda s: None if s is None else len(str(s)),
    "ABS": lambda x: None if x is None else abs(x),
    "ROUND": lambda x, d=0: None if x is None else round(x, int(d)),
    "COALESCE": lambda *xs: next((x for x in xs if x is not None), None),
}


def _compile_scalar_function(expr: ast.FuncCall, scope, params) -> Evaluator:
    fn = _SCALAR_FUNCTIONS.get(expr.name)
    if fn is None:
        raise ExecutionError(f"unknown function {expr.name}()")
    args = [compile_expr(a, scope, params) for a in expr.args]

    def call(row: Row) -> Any:
        try:
            return fn(*(a(row) for a in args))
        except TypeError as exc:
            raise ExecutionError(f"bad arguments to {expr.name}(): {exc}") from None

    return call
