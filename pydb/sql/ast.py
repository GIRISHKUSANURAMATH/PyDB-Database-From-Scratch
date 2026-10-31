"""Abstract syntax tree produced by the parser.

Expressions and statements are plain frozen dataclasses: structural equality
lets the planner match ``GROUP BY age`` against ``age`` in the SELECT list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ============================================================================ expressions


class Expr:
    """Base class for expression nodes."""


@dataclass(frozen=True)
class Literal(Expr):
    value: Any


@dataclass(frozen=True)
class Param(Expr):
    index: int  # position of the ``?`` in the statement


@dataclass(frozen=True)
class ColumnRef(Expr):
    name: str
    table: str | None = None  # qualifier, e.g. users.name

    def __str__(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Star(Expr):
    table: str | None = None  # SELECT * or SELECT users.*


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str  # NOT, -, +
    operand: Expr


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str  # AND OR = != < <= > >= + - * / % LIKE
    left: Expr
    right: Expr


@dataclass(frozen=True)
class IsNull(Expr):
    operand: Expr
    negated: bool = False


@dataclass(frozen=True)
class InList(Expr):
    operand: Expr
    items: tuple[Expr, ...]
    negated: bool = False


@dataclass(frozen=True)
class Between(Expr):
    operand: Expr
    low: Expr
    high: Expr
    negated: bool = False


@dataclass(frozen=True)
class FuncCall(Expr):
    name: str  # upper-case
    args: tuple[Expr, ...]
    star: bool = False  # COUNT(*)
    distinct: bool = False


AGGREGATES = frozenset({"COUNT", "SUM", "AVG", "MIN", "MAX"})

# ============================================================================ statements


class Statement:
    pass


@dataclass
class ColumnDef:
    name: str
    type_name: str
    not_null: bool = False
    unique: bool = False
    primary_key: bool = False
    default: Expr | None = None
    references: tuple[str, str] | None = None


@dataclass
class CreateTable(Statement):
    name: str
    columns: list[ColumnDef]
    if_not_exists: bool = False


@dataclass
class DropTable(Statement):
    name: str
    if_exists: bool = False


@dataclass
class CreateIndex(Statement):
    name: str
    table: str
    column: str
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class DropIndex(Statement):
    name: str
    if_exists: bool = False


@dataclass
class Insert(Statement):
    table: str
    columns: list[str] | None
    rows: list[list[Expr]]


@dataclass
class SelectItem:
    expr: Expr
    alias: str | None = None


@dataclass
class TableRef:
    name: str
    alias: str | None = None

    @property
    def ref_name(self) -> str:
        return self.alias or self.name


@dataclass
class Join:
    table: TableRef
    condition: Expr | None  # None for CROSS JOIN
    kind: str = "INNER"


@dataclass
class OrderItem:
    expr: Expr
    descending: bool = False


@dataclass
class Select(Statement):
    items: list[SelectItem]
    from_table: TableRef | None
    joins: list[Join] = field(default_factory=list)
    where: Expr | None = None
    group_by: list[Expr] = field(default_factory=list)
    having: Expr | None = None
    order_by: list[OrderItem] = field(default_factory=list)
    limit: Expr | None = None
    offset: Expr | None = None
    distinct: bool = False


@dataclass
class Update(Statement):
    table: str
    assignments: list[tuple[str, Expr]]
    where: Expr | None = None


@dataclass
class Delete(Statement):
    table: str
    where: Expr | None = None


@dataclass
class Begin(Statement):
    pass


@dataclass
class Commit(Statement):
    pass


@dataclass
class Rollback(Statement):
    pass


@dataclass
class Explain(Statement):
    statement: Statement


# ============================================================================ helpers


def expr_to_sql(e: Expr) -> str:
    """Render an expression back to SQL — used for column headers and EXPLAIN."""
    if isinstance(e, Literal):
        v = e.value
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return repr(v)
    if isinstance(e, Param):
        return "?"
    if isinstance(e, ColumnRef):
        return str(e)
    if isinstance(e, Star):
        return f"{e.table}.*" if e.table else "*"
    if isinstance(e, UnaryOp):
        return f"NOT {expr_to_sql(e.operand)}" if e.op == "NOT" else f"{e.op}{expr_to_sql(e.operand)}"
    if isinstance(e, BinaryOp):
        return f"{_paren(e.left)} {e.op} {_paren(e.right)}"
    if isinstance(e, IsNull):
        return f"{expr_to_sql(e.operand)} IS {'NOT ' if e.negated else ''}NULL"
    if isinstance(e, InList):
        items = ", ".join(expr_to_sql(i) for i in e.items)
        return f"{expr_to_sql(e.operand)} {'NOT ' if e.negated else ''}IN ({items})"
    if isinstance(e, Between):
        neg = "NOT " if e.negated else ""
        return f"{expr_to_sql(e.operand)} {neg}BETWEEN {expr_to_sql(e.low)} AND {expr_to_sql(e.high)}"
    if isinstance(e, FuncCall):
        if e.star:
            return f"{e.name}(*)"
        inner = ", ".join(expr_to_sql(a) for a in e.args)
        return f"{e.name}({'DISTINCT ' if e.distinct else ''}{inner})"
    return repr(e)  # pragma: no cover


def _paren(e: Expr) -> str:
    s = expr_to_sql(e)
    return f"({s})" if isinstance(e, BinaryOp) and e.op in ("AND", "OR") else s


def walk(e: Expr):
    """Yield every node of an expression tree (pre-order)."""
    yield e
    if isinstance(e, UnaryOp):
        yield from walk(e.operand)
    elif isinstance(e, BinaryOp):
        yield from walk(e.left)
        yield from walk(e.right)
    elif isinstance(e, IsNull):
        yield from walk(e.operand)
    elif isinstance(e, InList):
        yield from walk(e.operand)
        for item in e.items:
            yield from walk(item)
    elif isinstance(e, Between):
        yield from walk(e.operand)
        yield from walk(e.low)
        yield from walk(e.high)
    elif isinstance(e, FuncCall):
        for a in e.args:
            yield from walk(a)


def contains_aggregate(e: Expr) -> bool:
    return any(isinstance(n, FuncCall) and n.name in AGGREGATES for n in walk(e))
