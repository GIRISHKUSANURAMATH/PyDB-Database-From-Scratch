"""Query planner: AST -> tree of physical operators.

This is a small **rule-based optimizer**. The rules, in order:

1. **Predicate pushdown** — the WHERE clause is split into AND-ed conjuncts.
   A conjunct that references a single table is attached directly to that
   table's scan, so rows are discarded before they reach a join.
2. **Access path selection** — for each table, look for a *sargable*
   conjunct (``col = const``, ``col < const``, ``col BETWEEN a AND b``, ...)
   on an indexed column. Equality beats ranges; unique indexes beat
   non-unique ones; several range conjuncts on the same column are merged into
   one ``[low, high]`` scan. If nothing qualifies, use a sequential scan.
3. **Join algorithm selection** — for an equi-join (``a.x = b.y``):
   index nested-loop join if the inner table has an index on ``y``, otherwise
   a hash join. Any other condition falls back to a nested-loop join.
4. Then aggregation, HAVING, ORDER BY, projection, DISTINCT, LIMIT.

``EXPLAIN`` prints the resulting operator tree.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydb.catalog.schema import IndexInfo, TableSchema
from pydb.errors import ColumnNotFoundError, ExecutionError, TableNotFoundError
from pydb.execution.aggregate import AggSpec, HashAggregate
from pydb.execution.filter import Filter
from pydb.execution.join import HashJoin, IndexNestedLoopJoin, NestedLoopJoin
from pydb.execution.operators import Operator
from pydb.execution.projection import Projection
from pydb.execution.scan import IndexScan, SeqScan, SingleRow, SystemTableScan, table_bindings
from pydb.execution.sort import Distinct, Limit, Sort
from pydb.sql import ast
from pydb.sql.expressions import Binding, Slot, compile_expr, evaluate_constant, resolve
from pydb.storage.record import ColumnType

if TYPE_CHECKING:
    from pydb.engine import Engine

_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


# ============================================================================ helpers
def split_conjuncts(expr: ast.Expr | None) -> list[ast.Expr]:
    if expr is None:
        return []
    if isinstance(expr, ast.BinaryOp) and expr.op == "AND":
        return split_conjuncts(expr.left) + split_conjuncts(expr.right)
    return [expr]


def join_conjuncts(parts: Sequence[ast.Expr]) -> ast.Expr | None:
    out: ast.Expr | None = None
    for p in parts:
        out = p if out is None else ast.BinaryOp("AND", out, p)
    return out


def column_refs(expr: ast.Expr) -> list[ast.ColumnRef]:
    return [n for n in ast.walk(expr) if isinstance(n, ast.ColumnRef)]


def is_constant(expr: ast.Expr) -> bool:
    return not any(isinstance(n, (ast.ColumnRef, ast.Star, Slot)) for n in ast.walk(expr)) and not any(
        isinstance(n, ast.FuncCall) and n.name in ast.AGGREGATES for n in ast.walk(expr)
    )


def _key_compatible(value: Any, col_type: ColumnType) -> bool:
    if value is None:
        return False
    if col_type in (ColumnType.INTEGER, ColumnType.FLOAT):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if col_type is ColumnType.TEXT:
        return isinstance(value, str)
    return isinstance(value, bool)


@dataclass
class Source:
    """One table in the FROM clause during planning."""

    ref: ast.TableRef
    schema: TableSchema
    bindings: list[Binding]
    nullable: bool = False  # right side of a LEFT JOIN: WHERE must not be pushed below the join
    pushed: list[ast.Expr] = field(default_factory=list)

    @property
    def alias(self) -> str:
        return self.ref.ref_name


@dataclass
class AccessPath:
    index: IndexInfo | None = None
    low: Any = None
    high: Any = None
    low_inclusive: bool = True
    high_inclusive: bool = True
    used: list[ast.Expr] = field(default_factory=list)
    description: str = ""
    is_equality: bool = False


# ============================================================================ planner
class Planner:
    def __init__(self, engine: Engine, params: Sequence[Any] = ()) -> None:
        self.engine = engine
        self.params = params

    def compile(self, expr: ast.Expr, scope: Sequence[Binding]):
        return compile_expr(expr, scope, self.params)

    # ---------------------------------------------------------------- sources
    def _source(self, ref: ast.TableRef) -> Source:
        schema = self.engine.catalog.get_table(ref.name)
        return Source(ref, schema, table_bindings(ref.ref_name, schema.column_names))

    def _scan(self, src: Source) -> Operator:
        """Build the access path + pushed-down filter for one table."""
        if src.schema.system:
            rows = self.engine.catalog.system_rows(src.schema.name)
            op: Operator = SystemTableScan(src.schema.name, src.alias, src.schema.column_names, rows)
            remaining = src.pushed
        else:
            path = self.choose_access_path(src.schema, src.alias, src.bindings, src.pushed)
            access = self.engine.table(src.schema)
            if path.index is not None:
                op = IndexScan(access, src.alias, path.index, self.engine.indexes, path.low, path.high,
                               path.low_inclusive, path.high_inclusive, path.description)
            else:
                op = SeqScan(access, src.alias, self.engine.row_count(src.schema.id))
            remaining = [c for c in src.pushed if not any(c is u for u in path.used)]
        if remaining:
            pred = join_conjuncts(remaining)
            op = Filter(op, self.compile(pred, op.columns), ast.expr_to_sql(pred))
        return op

    def choose_access_path(
        self, schema: TableSchema, alias: str, scope: Sequence[Binding], conjuncts: Sequence[ast.Expr]
    ) -> AccessPath:
        """Pick the best index for a set of single-table conjuncts (rule-based)."""
        # column -> list of (op, value, conjunct)
        bounds: dict[str, list[tuple[str, Any, ast.Expr]]] = {}
        for c in conjuncts:
            for col, op, value in self._sargable(c, alias, scope):
                if schema.index_on(col) is None or not _key_compatible(value, schema.column(col).type):
                    continue
                bounds.setdefault(col, []).append((op, value, c))
        best: AccessPath | None = None
        for col, items in bounds.items():
            info = schema.index_on(col)
            path = self._path_for(col, info, items)
            if path is None:
                continue
            rank = (path.is_equality, info.unique)
            if best is None or rank > (best.is_equality, best.index.unique):
                best = path
        return best or AccessPath()

    def _sargable(self, c: ast.Expr, alias: str, scope: Sequence[Binding]) -> list[tuple[str, str, Any]]:
        def own_column(e: ast.Expr) -> str | None:
            if isinstance(e, ast.ColumnRef):
                try:
                    idx = resolve(e, scope)
                except ColumnNotFoundError:
                    return None
                return scope[idx].name if scope[idx].table == alias else None
            return None

        if isinstance(c, ast.BinaryOp) and c.op in _FLIP:
            col = own_column(c.left)
            if col is not None and is_constant(c.right):
                return [(col, c.op, evaluate_constant(c.right, self.params))]
            col = own_column(c.right)
            if col is not None and is_constant(c.left):
                return [(col, _FLIP[c.op], evaluate_constant(c.left, self.params))]
        if isinstance(c, ast.Between) and not c.negated:
            col = own_column(c.operand)
            if col is not None and is_constant(c.low) and is_constant(c.high):
                return [(col, ">=", evaluate_constant(c.low, self.params)),
                        (col, "<=", evaluate_constant(c.high, self.params))]
        return []

    @staticmethod
    def _path_for(col: str, info: IndexInfo, items: list[tuple[str, Any, ast.Expr]]) -> AccessPath | None:
        for op, value, conj in items:
            if op == "=":
                return AccessPath(info, value, value, True, True, [conj], f"{col} = {value!r}", True)
        path = AccessPath(info)
        used: list[ast.Expr] = []
        for op, value, conj in items:
            if op in (">", ">="):
                if path.low is None or value > path.low or (value == path.low and op == ">"):
                    path.low, path.low_inclusive = value, op == ">="
            else:
                if path.high is None or value < path.high or (value == path.high and op == "<"):
                    path.high, path.high_inclusive = value, op == "<="
            if not any(conj is u for u in used):
                used.append(conj)
        parts = []
        if path.low is not None:
            parts.append(f"{col} {'>=' if path.low_inclusive else '>'} {path.low!r}")
        if path.high is not None:
            parts.append(f"{col} {'<=' if path.high_inclusive else '<'} {path.high!r}")
        path.used = used
        path.description = " AND ".join(parts)
        return path

    # ---------------------------------------------------------------- SELECT
    def plan_select(self, stmt: ast.Select) -> Operator:
        if stmt.from_table is None:
            op: Operator = SingleRow()
            if stmt.where is not None:
                op = Filter(op, self.compile(stmt.where, []), ast.expr_to_sql(stmt.where))
            return self._finish_select(stmt, op)

        sources = [self._source(stmt.from_table)]
        for j in stmt.joins:
            src = self._source(j.table)
            src.nullable = j.kind == "LEFT"
            sources.append(src)
        aliases = [s.alias for s in sources]
        if len(set(aliases)) != len(aliases):
            raise ExecutionError("each table in FROM must have a distinct name; use aliases")

        full_scope = [b for s in sources for b in s.bindings]
        residual: list[ast.Expr] = []
        for conj in split_conjuncts(stmt.where):
            owner = self._single_owner(conj, sources, full_scope)
            if owner is not None and not owner.nullable:
                owner.pushed.append(conj)  # predicate pushdown
            else:
                residual.append(conj)

        op = self._scan(sources[0])
        left_aliases = {sources[0].alias}
        for j, src in zip(stmt.joins, sources[1:], strict=False):
            op = self._join(op, left_aliases, src, j)
            left_aliases.add(src.alias)
        if residual:
            pred = join_conjuncts(residual)
            op = Filter(op, self.compile(pred, op.columns), ast.expr_to_sql(pred))
        return self._finish_select(stmt, op)

    def _single_owner(self, conj: ast.Expr, sources: list[Source], scope: list[Binding]) -> Source | None:
        refs = column_refs(conj)
        if not refs or any(isinstance(n, ast.FuncCall) and n.name in ast.AGGREGATES for n in ast.walk(conj)):
            return None
        owners = {scope[resolve(r, scope)].table for r in refs}
        if len(owners) != 1:
            return None
        alias = owners.pop()
        return next(s for s in sources if s.alias == alias)

    def _join(self, left: Operator, left_aliases: set[str], src: Source, join: ast.Join) -> Operator:
        kind = "INNER" if join.kind == "CROSS" else join.kind
        conjuncts = split_conjuncts(join.condition)
        scope = left.columns + src.bindings
        equi: tuple[ast.Expr, ast.ColumnRef] | None = None
        rest: list[ast.Expr] = []
        for c in conjuncts:
            if equi is None and isinstance(c, ast.BinaryOp) and c.op == "=":
                pair = self._equi_sides(c, left_aliases, src.alias, scope)
                if pair is not None:
                    equi = pair
                    continue
            rest.append(c)
        residual = join_conjuncts(rest)
        residual_fn = self.compile(residual, scope) if residual is not None else None
        cond_text = ast.expr_to_sql(join.condition) if join.condition is not None else ""

        if equi is not None:
            left_expr, right_col = equi
            right_name = src.bindings[resolve(right_col, src.bindings)].name
            info = None if src.schema.system or src.pushed else src.schema.index_on(right_name)
            left_key = self.compile(left_expr, left.columns)
            if info is not None:
                return IndexNestedLoopJoin(left, self.engine.table(src.schema), src.bindings, info,
                                           self.engine.indexes, left_key, residual_fn, kind, cond_text)
            right = self._scan(src)
            right_key = self.compile(right_col, right.columns)
            return HashJoin(left, right, left_key, right_key, residual_fn, kind, cond_text)
        right = self._scan(src)
        cond = self.compile(join.condition, scope) if join.condition is not None else None
        return NestedLoopJoin(left, right, cond, join.kind if join.kind == "CROSS" else kind, cond_text)

    @staticmethod
    def _equi_sides(c: ast.BinaryOp, left_aliases: set[str], right_alias: str, scope) -> tuple | None:
        def owners(e: ast.Expr) -> set[str]:
            return {scope[resolve(r, scope)].table for r in column_refs(e)}

        lo, ro = owners(c.left), owners(c.right)
        if isinstance(c.right, ast.ColumnRef) and ro == {right_alias} and lo and lo <= left_aliases:
            return c.left, c.right
        if isinstance(c.left, ast.ColumnRef) and lo == {right_alias} and ro and ro <= left_aliases:
            return c.right, c.left
        return None

    # ---------------------------------------------------------------- aggregation / output
    def _finish_select(self, stmt: ast.Select, op: Operator) -> Operator:
        items = self._expand_stars(stmt.items, op.columns)
        names = [self._item_name(it) for it in items]
        order_exprs = [self._order_target(o.expr, items) for o in stmt.order_by]

        needs_agg = bool(stmt.group_by) or any(
            ast.contains_aggregate(e) for e in [it.expr for it in items] + order_exprs + (
                [stmt.having] if stmt.having else [])
        )
        if needs_agg:
            op, rewrite = self._aggregate(stmt, op, [it.expr for it in items], order_exprs)
            item_exprs = [rewrite(it.expr) for it in items]
            order_exprs = [rewrite(e) for e in order_exprs]
            if stmt.having is not None:
                having = rewrite(stmt.having)
                op = Filter(op, self.compile(having, op.columns), "HAVING " + ast.expr_to_sql(stmt.having))
        else:
            if stmt.having is not None:
                raise ExecutionError("HAVING requires GROUP BY or an aggregate")
            item_exprs = [it.expr for it in items]

        if stmt.order_by:
            keys = [(self.compile(e, op.columns), o.descending)
                    for e, o in zip(order_exprs, stmt.order_by, strict=False)]
            desc = ", ".join(ast.expr_to_sql(o.expr) + (" DESC" if o.descending else "") for o in stmt.order_by)
            op = Sort(op, keys, desc)
        op = Projection(op, [self.compile(e, op.columns) for e in item_exprs], names)
        if stmt.distinct:
            op = Distinct(op)
        if stmt.limit is not None or stmt.offset is not None:
            limit = self._int_clause(stmt.limit, "LIMIT") if stmt.limit is not None else None
            offset = self._int_clause(stmt.offset, "OFFSET") if stmt.offset is not None else 0
            op = Limit(op, limit, offset)
        return op

    def _int_clause(self, expr: ast.Expr, what: str) -> int:
        value = evaluate_constant(expr, self.params)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExecutionError(f"{what} must be a non-negative integer")
        return value

    @staticmethod
    def _expand_stars(items: list[ast.SelectItem], scope: list[Binding]) -> list[ast.SelectItem]:
        out: list[ast.SelectItem] = []
        for it in items:
            if isinstance(it.expr, ast.Star):
                matched = [b for b in scope if it.expr.table is None or b.table == it.expr.table]
                if it.expr.table is not None and not matched:
                    raise TableNotFoundError(f"no table named {it.expr.table} in FROM")
                if not scope:
                    raise ExecutionError("SELECT * requires a FROM clause")
                out.extend(ast.SelectItem(ast.ColumnRef(b.name, b.table)) for b in matched)
            else:
                out.append(it)
        return out

    @staticmethod
    def _item_name(it: ast.SelectItem) -> str:
        if it.alias:
            return it.alias
        if isinstance(it.expr, ast.ColumnRef):
            return it.expr.name
        return ast.expr_to_sql(it.expr)

    def _order_target(self, expr: ast.Expr, items: list[ast.SelectItem]) -> ast.Expr:
        """ORDER BY may name a SELECT alias or a 1-based column position."""
        if isinstance(expr, ast.Literal) and isinstance(expr.value, int) and not isinstance(expr.value, bool):
            if not 1 <= expr.value <= len(items):
                raise ExecutionError(f"ORDER BY position {expr.value} is out of range")
            return items[expr.value - 1].expr
        if isinstance(expr, ast.ColumnRef) and expr.table is None:
            for it in items:
                if it.alias == expr.name:
                    return it.expr
        return expr

    def _aggregate(self, stmt: ast.Select, op: Operator, item_exprs: list[ast.Expr], order_exprs: list[ast.Expr]):
        scope = op.columns
        for e in item_exprs:
            if isinstance(e, ast.Star):  # pragma: no cover - stars already expanded
                raise ExecutionError("SELECT * cannot be combined with GROUP BY")
        group_exprs = list(stmt.group_by)
        group_evals = [self.compile(g, scope) for g in group_exprs]
        group_cols = [resolve(g, scope) if isinstance(g, ast.ColumnRef) else None for g in group_exprs]

        agg_calls: list[ast.FuncCall] = []
        for e in [*item_exprs, *order_exprs, *([stmt.having] if stmt.having else [])]:
            for node in ast.walk(e):
                if isinstance(node, ast.FuncCall) and node.name in ast.AGGREGATES and node not in agg_calls:
                    for arg in node.args:
                        if ast.contains_aggregate(arg):
                            raise ExecutionError("aggregate functions cannot be nested")
                    agg_calls.append(node)
        specs = []
        for call in agg_calls:
            if not call.star and len(call.args) != 1:
                raise ExecutionError(f"{call.name}() takes exactly one argument")
            arg = None if call.star else self.compile(call.args[0], scope)
            specs.append(AggSpec(call.name, arg, call.distinct, ast.expr_to_sql(call)))
        group_labels = [ast.expr_to_sql(g) for g in group_exprs]
        agg_op = HashAggregate(op, group_evals, group_labels, specs)
        n_groups = len(group_exprs)

        def rewrite(e: ast.Expr) -> ast.Expr:
            for i, g in enumerate(group_exprs):
                if e == g:
                    return Slot(i, group_labels[i])
            if isinstance(e, ast.ColumnRef):
                idx = resolve(e, scope)
                for i, gc in enumerate(group_cols):
                    if gc == idx:
                        return Slot(i, group_labels[i])
                raise ExecutionError(f"column {e} must appear in GROUP BY or be used in an aggregate function")
            if isinstance(e, ast.FuncCall) and e.name in ast.AGGREGATES:
                j = agg_calls.index(e)
                return Slot(n_groups + j, specs[j].label)
            if isinstance(e, ast.UnaryOp):
                return ast.UnaryOp(e.op, rewrite(e.operand))
            if isinstance(e, ast.BinaryOp):
                return ast.BinaryOp(e.op, rewrite(e.left), rewrite(e.right))
            if isinstance(e, ast.IsNull):
                return ast.IsNull(rewrite(e.operand), e.negated)
            if isinstance(e, ast.InList):
                return ast.InList(rewrite(e.operand), tuple(rewrite(i) for i in e.items), e.negated)
            if isinstance(e, ast.Between):
                return ast.Between(rewrite(e.operand), rewrite(e.low), rewrite(e.high), e.negated)
            if isinstance(e, ast.FuncCall):
                return ast.FuncCall(e.name, tuple(rewrite(a) for a in e.args), e.star, e.distinct)
            return e

        return agg_op, rewrite

    # ---------------------------------------------------------------- UPDATE / DELETE
    def plan_dml_scan(self, schema: TableSchema, where: ast.Expr | None) -> tuple[Operator, Any, Operator]:
        """Access path for UPDATE/DELETE: (scan, residual predicate or None, plan for EXPLAIN)."""
        bindings = table_bindings(schema.name, schema.column_names)
        conjuncts = split_conjuncts(where)
        path = self.choose_access_path(schema, schema.name, bindings, conjuncts)
        access = self.engine.table(schema)
        if path.index is not None:
            scan: Operator = IndexScan(access, schema.name, path.index, self.engine.indexes, path.low, path.high,
                                       path.low_inclusive, path.high_inclusive, path.description)
        else:
            scan = SeqScan(access, schema.name, self.engine.row_count(schema.id))
        remaining = [c for c in conjuncts if not any(c is u for u in path.used)]
        pred_expr = join_conjuncts(remaining)
        pred = self.compile(pred_expr, bindings) if pred_expr is not None else None
        if pred_expr is not None:
            scan_for_explain: Operator = Filter(scan, pred, ast.expr_to_sql(pred_expr))
        else:
            scan_for_explain = scan
        return scan, pred, scan_for_explain
