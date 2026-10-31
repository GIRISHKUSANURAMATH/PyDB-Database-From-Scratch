"""Statement execution: sessions, transactions-per-statement and results.

A :class:`Session` is one client connection. It owns at most one explicit
transaction (``BEGIN`` ... ``COMMIT``); outside of one, every statement runs in
its own *autocommit* transaction.

Execution of one statement::

    parse -> acquire table locks (may wait; no latch held)
          -> take the engine latch
          -> remember a savepoint (length of the undo list)
          -> plan + execute
          -> on error: roll back to the savepoint (statement atomicity)
          -> autocommit: COMMIT (fsync WAL) and release locks
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydb.catalog.schema import Column
from pydb.constraints.constraints import build_row, coerce_row
from pydb.errors import (
    ColumnNotFoundError,
    DatabaseError,
    ExecutionError,
    LockError,
    TableAlreadyExistsError,
    TableNotFoundError,
    TransactionError,
)
from pydb.execution.planner import Planner
from pydb.sql import ast
from pydb.sql.expressions import compile_expr, evaluate_constant
from pydb.sql.parser import parse, parse_one
from pydb.storage.record import ColumnType
from pydb.transaction.lock_manager import LockMode
from pydb.transaction.transaction import Transaction

if TYPE_CHECKING:
    from pydb.engine import Engine

log = logging.getLogger(__name__)

CATALOG_LOCK = "__catalog__"


@dataclass
class Result:
    """Outcome of one statement. Iterating yields the result rows as tuples."""

    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0
    message: str = ""
    plan: str | None = None

    def __iter__(self) -> Iterator[tuple]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def fetchall(self) -> list[tuple]:
        return list(self.rows)

    def fetchone(self) -> tuple | None:
        return self.rows[0] if self.rows else None

    def scalar(self) -> Any:
        return self.rows[0][0] if self.rows and self.rows[0] else None

    def as_dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r, strict=False)) for r in self.rows]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class Session:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.txn: Transaction | None = None

    @property
    def in_transaction(self) -> bool:
        return self.txn is not None

    # ================================================================== public
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        stmt, n_params = parse_one(sql)
        params = tuple(params)
        if n_params != len(params):
            raise ExecutionError(f"statement has {n_params} parameter(s) but {len(params)} were supplied")
        return self.execute_statement(stmt, params)

    def execute_script(self, sql: str) -> list[Result]:
        return [self.execute_statement(s, ()) for s in parse(sql)]

    def execute_statement(self, stmt: ast.Statement, params: Sequence[Any] = ()) -> Result:
        if self.engine.closed:
            raise DatabaseError("database is closed")
        if isinstance(stmt, ast.Begin):
            return self._begin()
        if isinstance(stmt, ast.Commit):
            return self._commit()
        if isinstance(stmt, ast.Rollback):
            return self._rollback()
        return self._run(stmt, params)

    # ================================================================== transactions
    def _begin(self) -> Result:
        if self.txn is not None:
            raise TransactionError("a transaction is already active")
        self.txn = self.engine.txm.begin(explicit=True)
        return Result(message="Transaction started.")

    def _commit(self) -> Result:
        if self.txn is None:
            raise TransactionError("no transaction is active")
        txn, self.txn = self.txn, None
        with self.engine.latch:
            self.engine.txm.commit(txn)
            self.engine.maybe_checkpoint()
        return Result(message="Transaction committed.")

    def _rollback(self) -> Result:
        if self.txn is None:
            raise TransactionError("no transaction is active")
        txn, self.txn = self.txn, None
        with self.engine.latch:
            self.engine.txm.rollback(txn)
        return Result(message="Transaction rolled back.")

    def close(self) -> None:
        if self.txn is not None and not self.engine.closed:
            self._rollback()

    # ================================================================== statement driver
    def _run(self, stmt: ast.Statement, params: Sequence[Any]) -> Result:
        engine = self.engine
        is_ddl = isinstance(stmt, (ast.CreateTable, ast.DropTable, ast.CreateIndex, ast.DropIndex))
        if is_ddl and self.txn is not None:
            raise TransactionError("DDL statements (CREATE/DROP) cannot run inside an explicit transaction")
        autocommit = self.txn is None
        txn = self.txn or engine.txm.begin()
        try:
            self._acquire_locks(txn, stmt, is_ddl)
            with engine.latch:
                savepoint = len(txn.undo)
                try:
                    result = self._dispatch(txn, stmt, params)
                except Exception:
                    if txn.is_active and len(txn.undo) > savepoint:
                        engine.txm.rollback_to(txn, savepoint)  # undo this statement only
                    raise
                if autocommit:
                    engine.txm.commit(txn)
                    engine.maybe_checkpoint()
            return result
        except Exception as exc:
            if txn.is_active and (autocommit or isinstance(exc, LockError)):
                # Autocommit statements roll back entirely; an explicit transaction that
                # lost a deadlock/timeout is aborted so its locks are released.
                with engine.latch:
                    engine.txm.rollback(txn)
                if not autocommit:
                    self.txn = None
            raise

    def _acquire_locks(self, txn: Transaction, stmt: ast.Statement, is_ddl: bool) -> None:
        locks = self.engine.locks
        if is_ddl:
            locks.acquire(txn.txn_id, CATALOG_LOCK, LockMode.EXCLUSIVE)
            return
        locks.acquire(txn.txn_id, CATALOG_LOCK, LockMode.SHARED)
        target = stmt.statement if isinstance(stmt, ast.Explain) else stmt
        reads, writes = self._tables_touched(target)
        if isinstance(stmt, ast.Explain):
            reads, writes = reads | writes, set()
        for name in sorted(writes):
            locks.acquire(txn.txn_id, "table:" + name, LockMode.EXCLUSIVE)
        for name in sorted(reads - writes):
            locks.acquire(txn.txn_id, "table:" + name, LockMode.SHARED)

    def _tables_touched(self, stmt: ast.Statement) -> tuple[set[str], set[str]]:
        catalog = self.engine.catalog
        reads: set[str] = set()
        writes: set[str] = set()
        if isinstance(stmt, ast.Select):
            if stmt.from_table:
                reads.add(stmt.from_table.name)
            reads.update(j.table.name for j in stmt.joins)
        elif isinstance(stmt, (ast.Insert, ast.Update, ast.Delete)):
            writes.add(stmt.table.lower())
            with self.engine.latch:
                if catalog.has_table(stmt.table):  # FK checks read related tables
                    schema = catalog.get_table(stmt.table)
                    reads.update(c.references[0] for c in schema.columns if c.references)
                    for child in catalog.tables.values():
                        if any(c.references and c.references[0] == schema.name for c in child.columns):
                            reads.add(child.name)
        return reads, writes

    def _dispatch(self, txn: Transaction, stmt: ast.Statement, params: Sequence[Any]) -> Result:
        if isinstance(stmt, ast.Select):
            return self._select(stmt, params)
        if isinstance(stmt, ast.Insert):
            return self._insert(txn, stmt, params)
        if isinstance(stmt, ast.Update):
            return self._update(txn, stmt, params)
        if isinstance(stmt, ast.Delete):
            return self._delete(txn, stmt, params)
        if isinstance(stmt, ast.CreateTable):
            return self._create_table(txn, stmt)
        if isinstance(stmt, ast.DropTable):
            return self._drop_table(txn, stmt)
        if isinstance(stmt, ast.CreateIndex):
            return self._create_index(txn, stmt)
        if isinstance(stmt, ast.DropIndex):
            return self._drop_index(txn, stmt)
        if isinstance(stmt, ast.Explain):
            return self._explain(stmt, params)
        raise ExecutionError(f"unsupported statement {type(stmt).__name__}")  # pragma: no cover

    # ================================================================== queries
    def _select(self, stmt: ast.Select, params: Sequence[Any]) -> Result:
        plan = Planner(self.engine, params).plan_select(stmt)
        rows = list(plan)
        names = [b.name for b in plan.columns]
        return Result(names, rows, len(rows), f"{_plural(len(rows), 'row')} returned.")

    def _explain(self, stmt: ast.Explain, params: Sequence[Any]) -> Result:
        inner = stmt.statement
        planner = Planner(self.engine, params)
        if isinstance(inner, ast.Select):
            text = planner.plan_select(inner).explain()
        else:
            schema = self.engine.catalog.get_table(inner.table)
            _scan, _pred, shown = planner.plan_dml_scan(schema, inner.where)
            verb = "Update" if isinstance(inner, ast.Update) else "Delete"
            text = "\n".join([f"{verb}({schema.name})", *shown.explain_lines(1)])
        lines = text.splitlines()
        return Result(["plan"], [(ln,) for ln in lines], len(lines), "", plan=text)

    # ================================================================== DML
    def _insert(self, txn: Transaction, stmt: ast.Insert, params: Sequence[Any]) -> Result:
        schema = self.engine.catalog.get_table(stmt.table)
        table = self.engine.table(schema)
        if stmt.columns is not None:
            for c in stmt.columns:
                if not schema.has_column(c):
                    raise ColumnNotFoundError(f"table {schema.name} has no column {c}")
            if len(set(stmt.columns)) != len(stmt.columns):
                raise ExecutionError("a column is listed more than once")
        count = 0
        for values_ast in stmt.rows:
            values = [evaluate_constant(v, params) for v in values_ast]
            if stmt.columns is None:
                if len(values) != len(schema.columns):
                    raise ExecutionError(
                        f"table {schema.name} has {len(schema.columns)} columns but {len(values)} values were supplied"
                    )
                row = build_row(schema, dict(zip(schema.column_names, values, strict=False)))
            else:
                if len(values) != len(stmt.columns):
                    raise ExecutionError(f"{len(stmt.columns)} columns but {len(values)} values")
                row = build_row(schema, dict(zip(stmt.columns, values, strict=False)))
            table.insert(txn, row)
            count += 1
        return Result(rowcount=count, message=f"{_plural(count, 'row')} inserted.")

    def _update(self, txn: Transaction, stmt: ast.Update, params: Sequence[Any]) -> Result:
        schema = self.engine.catalog.get_table(stmt.table)
        table = self.engine.table(schema)
        planner = Planner(self.engine, params)
        scan, pred, _ = planner.plan_dml_scan(schema, stmt.where)
        bindings = scan.columns
        assigns = []
        for col, expr in stmt.assignments:
            assigns.append((schema.position(col), compile_expr(expr, bindings, params)))
        # Collect targets first so rows moved by the update are not visited twice.
        targets = [(rid, row) for rid, row in scan.rid_rows() if pred is None or pred(row) is True]
        for rid, row in targets:
            new = list(row)
            for pos, fn in assigns:
                new[pos] = fn(row)
            table.update(txn, rid, row, coerce_row(schema, new))
        return Result(rowcount=len(targets), message=f"{_plural(len(targets), 'row')} updated.")

    def _delete(self, txn: Transaction, stmt: ast.Delete, params: Sequence[Any]) -> Result:
        schema = self.engine.catalog.get_table(stmt.table)
        table = self.engine.table(schema)
        scan, pred, _ = Planner(self.engine, params).plan_dml_scan(schema, stmt.where)
        targets = [(rid, row) for rid, row in scan.rid_rows() if pred is None or pred(row) is True]
        for rid, row in targets:
            table.delete(txn, rid, row)
        return Result(rowcount=len(targets), message=f"{_plural(len(targets), 'row')} deleted.")

    # ================================================================== DDL
    def _create_table(self, txn: Transaction, stmt: ast.CreateTable) -> Result:
        if self.engine.catalog.has_table(stmt.name):
            if stmt.if_not_exists:
                return Result(message="Table already exists, skipped.")
            raise TableAlreadyExistsError(f"table {stmt.name} already exists")
        columns = []
        for cd in stmt.columns:
            default = evaluate_constant(cd.default) if cd.default is not None else None
            columns.append(Column(
                cd.name, ColumnType.parse(cd.type_name), cd.not_null or cd.primary_key, cd.unique,
                cd.primary_key, cd.default is not None, default, cd.references,
            ))
        self.engine.create_table(txn, stmt.name, columns)
        return Result(message="Table created.")

    def _drop_table(self, txn: Transaction, stmt: ast.DropTable) -> Result:
        if not self.engine.catalog.has_table(stmt.name):
            if stmt.if_exists:
                return Result(message="Table does not exist, skipped.")
            raise TableNotFoundError(f"no such table: {stmt.name}")
        self.engine.drop_table(txn, stmt.name)
        return Result(message="Table dropped.")

    def _create_index(self, txn: Transaction, stmt: ast.CreateIndex) -> Result:
        if stmt.if_not_exists and stmt.name in self.engine.catalog.indexes:
            return Result(message="Index already exists, skipped.")
        self.engine.create_index(txn, stmt.table, stmt.name, stmt.column, unique=stmt.unique)
        return Result(message="Index created.")

    def _drop_index(self, txn: Transaction, stmt: ast.DropIndex) -> Result:
        if stmt.if_exists and stmt.name not in self.engine.catalog.indexes:
            return Result(message="Index does not exist, skipped.")
        self.engine.drop_index(txn, stmt.name)
        return Result(message="Index dropped.")
