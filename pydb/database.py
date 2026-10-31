"""Public Python API.

Example::

    from pydb import Database

    with Database("school.db") as db:
        db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, marks INTEGER)")
        db.execute("INSERT INTO students VALUES (?, ?, ?)", (1, "Girish", 90))
        for row in db.execute("SELECT * FROM students WHERE marks >= ?", (80,)):
            print(row)

Parameters (``?``) are bound as *values* inside the parsed AST — they are never
spliced into the SQL text — so they cannot change the structure of a query
(no SQL injection).

``Database`` also offers a few convenience methods (``create_table``,
``insert``, ``select``, ``update``, ``delete``) that build parameterized SQL
for you. For multi-threaded use, give each thread its own :meth:`session`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from pydb.config import EngineConfig
from pydb.engine import Engine
from pydb.errors import DatabaseError
from pydb.execution.executor import Result, Session
from pydb.sql.lexer import KEYWORDS

log = logging.getLogger(__name__)


def _ident(name: str) -> str:
    if not name.replace("_", "a").isalnum() or name[0].isdigit():
        raise DatabaseError(f"invalid identifier {name!r}")
    return f'"{name}"' if name.upper() in KEYWORDS else name


class Database:
    def __init__(self, path: str, config: EngineConfig | None = None, **options: Any) -> None:
        """Open (or create) the database stored in ``path`` (plus ``path.wal``).

        Keyword options override :class:`EngineConfig` fields, e.g.
        ``Database("x.db", buffer_pool_size=64, sync_commit=False)``.
        """
        config = config or EngineConfig()
        for key, value in options.items():
            if not hasattr(config, key):
                raise TypeError(f"unknown option {key!r}")
            setattr(config, key, value)
        self.engine = Engine(path, config)
        self._session = Session(self.engine)

    # ------------------------------------------------------------------ SQL
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Result:
        """Execute one SQL statement, with optional ``?`` parameters."""
        return self._session.execute(sql, params)

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence[Any]]) -> int:
        """Run one statement for many parameter tuples inside a single transaction."""
        total = 0
        with self.transaction():
            for params in seq_of_params:
                total += self._session.execute(sql, params).rowcount
        return total

    def executescript(self, sql: str) -> list[Result]:
        """Execute several ``;``-separated statements (no parameters)."""
        return self._session.execute_script(sql)

    def explain(self, sql: str, params: Sequence[Any] = ()) -> str:
        return self.execute("EXPLAIN " + sql, params).plan or ""

    def session(self) -> Session:
        """A new independent session (connection) — use one per thread."""
        return Session(self.engine)

    # ------------------------------------------------------------------ transactions
    def begin(self) -> None:
        self.execute("BEGIN")

    def commit(self) -> None:
        self.execute("COMMIT")

    def rollback(self) -> None:
        self.execute("ROLLBACK")

    @property
    def in_transaction(self) -> bool:
        return self._session.in_transaction

    @contextmanager
    def transaction(self) -> Iterator[Database]:
        """``with db.transaction():`` commits on success, rolls back on exception."""
        started = not self.in_transaction
        if started:
            self.begin()
        try:
            yield self
        except BaseException:
            if started and self.in_transaction:
                self.rollback()
            raise
        else:
            if started and self.in_transaction:
                self.commit()

    # ------------------------------------------------------------------ convenience API
    def create_table(self, name: str, columns: Mapping[str, str], if_not_exists: bool = False) -> None:
        """``create_table("users", {"id": "INTEGER PRIMARY KEY", "name": "TEXT"})``"""
        cols = ", ".join(f"{_ident(c)} {spec}" for c, spec in columns.items())
        ine = "IF NOT EXISTS " if if_not_exists else ""
        self.execute(f"CREATE TABLE {ine}{_ident(name)} ({cols})")

    def drop_table(self, name: str, if_exists: bool = False) -> None:
        self.execute(f"DROP TABLE {'IF EXISTS ' if if_exists else ''}{_ident(name)}")

    def insert(self, table: str, row: Mapping[str, Any]) -> None:
        cols = ", ".join(_ident(c) for c in row)
        marks = ", ".join("?" for _ in row)
        self.execute(f"INSERT INTO {_ident(table)} ({cols}) VALUES ({marks})", tuple(row.values()))

    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> int:
        with self.transaction():
            for row in rows:
                self.insert(table, row)
        return len(rows)

    def _where(self, where: Mapping[str, Any] | None) -> tuple[str, tuple]:
        if not where:
            return "", ()
        parts, params = [], []
        for col, value in where.items():
            if value is None:
                parts.append(f"{_ident(col)} IS NULL")
            else:
                parts.append(f"{_ident(col)} = ?")
                params.append(value)
        return " WHERE " + " AND ".join(parts), tuple(params)

    def select(self, table: str, where: Mapping[str, Any] | None = None,
               columns: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Rows as dictionaries; ``where`` is a dict of column equalities."""
        cols = ", ".join(_ident(c) for c in columns) if columns else "*"
        clause, params = self._where(where)
        return self.execute(f"SELECT {cols} FROM {_ident(table)}{clause}", params).as_dicts()

    def update(self, table: str, values: Mapping[str, Any], where: Mapping[str, Any] | None = None) -> int:
        sets = ", ".join(f"{_ident(c)} = ?" for c in values)
        clause, params = self._where(where)
        return self.execute(f"UPDATE {_ident(table)} SET {sets}{clause}", tuple(values.values()) + params).rowcount

    def delete(self, table: str, where: Mapping[str, Any] | None = None) -> int:
        clause, params = self._where(where)
        return self.execute(f"DELETE FROM {_ident(table)}{clause}", params).rowcount

    # ------------------------------------------------------------------ introspection
    def tables(self) -> list[str]:
        return sorted(self.engine.catalog.tables)

    def schema(self, table: str) -> str:
        return self.engine.catalog.get_table(table).ddl()

    def indexes(self, table: str | None = None) -> list[dict[str, Any]]:
        out = []
        for ix in sorted(self.engine.catalog.indexes.values(), key=lambda i: i.name):
            t = self.engine.catalog.table_by_id(ix.table_id)
            if table is None or (t is not None and t.name == table.lower()):
                tree = self.engine.indexes.tree(ix)
                out.append({"name": ix.name, "table": t.name if t else "?", "column": ix.column,
                            "unique": ix.unique, "auto": ix.auto, **tree.stats()})
        return out

    def stats(self) -> dict[str, Any]:
        return self.engine.stats()

    def checkpoint(self) -> None:
        self.engine.checkpoint()

    # ------------------------------------------------------------------ lifecycle
    @property
    def closed(self) -> bool:
        return self.engine.closed

    def close(self) -> None:
        """Roll back any open transaction, checkpoint and close the files."""
        if not self.engine.closed:
            self._session.close()
            self.engine.close()

    def _simulate_crash(self) -> None:
        """Testing aid: drop all in-memory state as if the process had been killed."""
        self.engine.simulate_crash()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return f"<pydb.Database {self.engine.path!r} ({state})>"
