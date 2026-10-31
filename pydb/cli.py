"""Interactive shell: ``python -m pydb [database-file]``.

SQL statements end with ``;`` and may span several lines. Lines starting with
a dot are shell commands (``.help`` lists them).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from collections.abc import Sequence
from typing import Any, TextIO

from pydb import __version__
from pydb.database import Database
from pydb.errors import DatabaseError
from pydb.execution.executor import Result
from pydb.sql.lexer import TokenType, tokenize

DEFAULT_DB = os.path.join("data", "pydb.db")

HELP = """\
Shell commands:
  .help                 show this help
  .tables               list tables
  .schema [TABLE]       show CREATE TABLE statements
  .indexes [TABLE]      list indexes with B+ tree statistics
  .explain SQL          show the query plan for a statement
  .btree INDEX          print the B+ tree structure of an index
  .stats                engine statistics (buffer pool, WAL, transactions)
  .wal                  show the records currently in the write-ahead log
  .checkpoint           flush all pages and truncate the WAL
  .timer on|off         show execution time of each statement
  .read FILE            execute SQL statements from a file
  .quit / .exit         leave the shell

SQL: CREATE/DROP TABLE, CREATE [UNIQUE] INDEX, DROP INDEX, INSERT, SELECT
(WHERE, JOIN, GROUP BY, HAVING, ORDER BY, LIMIT), UPDATE, DELETE,
BEGIN / COMMIT / ROLLBACK, EXPLAIN. End statements with ';'."""


def format_value(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return f"{v:.1f}" if v.is_integer() and abs(v) < 1e15 else repr(v)
    return str(v)


def format_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    cells = [[format_value(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep, "| " + " | ".join(c.ljust(w) for c, w in zip(columns, widths, strict=False)) + " |", sep]
    for row in cells:
        out.append("| " + " | ".join(c.ljust(w) for c, w in zip(row, widths, strict=False)) + " |")
    out.append(sep)
    return "\n".join(out)


def statement_complete(buffer: str) -> bool:
    """True when the buffer ends with a ``;`` outside of any string literal."""
    try:
        tokens = tokenize(buffer)
    except DatabaseError:
        return False  # e.g. an unterminated string: keep reading
    meaningful = [t for t in tokens if t.type is not TokenType.EOF]
    return bool(meaningful) and meaningful[-1].is_punct(";")


class Shell:
    def __init__(self, db: Database, out: TextIO | None = None, debug: bool = False) -> None:
        self.db = db
        self.out = out or sys.stdout
        self.debug = debug
        self.timer = False

    def print(self, text: str = "") -> None:
        print(text, file=self.out)

    # ------------------------------------------------------------------ SQL
    def run_sql(self, sql: str) -> None:
        start = time.perf_counter()
        try:
            for result in self.db.executescript(sql):
                self.show(result)
        except DatabaseError as exc:
            self.print(f"Error: {exc}\n")
            if self.debug:
                self.print(traceback.format_exc())
        except Exception as exc:  # an engine bug — still never crash the shell
            self.print(f"Internal error: {type(exc).__name__}: {exc}")
            if self.debug:
                self.print(traceback.format_exc())
        if self.timer:
            self.print(f"Run time: {(time.perf_counter() - start) * 1000:.2f} ms")

    def show(self, result: Result) -> None:
        if result.plan is not None:
            self.print("Query Plan")
            self.print("-" * 10)
            self.print(result.plan)
        elif result.columns:
            self.print(format_table(result.columns, result.rows))
            self.print(result.message)
        elif result.message:
            self.print(result.message)
        self.print()

    # ------------------------------------------------------------------ dot commands
    def run_command(self, line: str) -> bool:
        """Run a dot command. Returns False when the shell should exit."""
        cmd, _, arg = line.strip().partition(" ")
        arg = arg.strip()
        cmd = cmd.lower()
        try:
            if cmd in (".quit", ".exit"):
                return False
            if cmd == ".help":
                self.print(HELP)
            elif cmd == ".tables":
                tables = self.db.tables()
                self.print("\n".join(tables) if tables else "(no tables)")
            elif cmd == ".schema":
                names = [arg] if arg else self.db.tables()
                for name in names:
                    self.print(self.db.schema(name))
            elif cmd == ".indexes":
                ixs = self.db.indexes(arg or None)
                if not ixs:
                    self.print("(no indexes)")
                else:
                    cols = ["name", "table", "column", "unique", "height", "leaf_nodes", "entries"]
                    self.print(format_table(cols, [[ix[c] for c in cols] for ix in ixs]))
            elif cmd == ".explain":
                self.print(self.db.explain(arg.rstrip(";")))
            elif cmd == ".btree":
                info = self.db.engine.catalog.get_index(arg)
                self.print(self.db.engine.indexes.tree(info).dump())
            elif cmd == ".stats":
                self._print_stats(self.db.stats())
            elif cmd == ".wal":
                records = self.db.engine.wal.records()
                self.print("\n".join(r.describe() for r in records[-50:]) or "(WAL is empty)")
                if len(records) > 50:
                    self.print(f"... showing last 50 of {len(records)} records")
            elif cmd == ".checkpoint":
                self.db.checkpoint()
                self.print("Checkpoint complete.")
            elif cmd == ".timer":
                self.timer = arg.lower() == "on"
                self.print(f"Timer {'on' if self.timer else 'off'}.")
            elif cmd == ".read":
                with open(arg, encoding="utf-8") as fh:
                    self.run_sql(fh.read())
            else:
                self.print(f"Unknown command {cmd}. Type .help for help.")
        except (DatabaseError, OSError) as exc:
            self.print(f"Error: {exc}")
            if self.debug:
                self.print(traceback.format_exc())
        return True

    def _print_stats(self, stats: dict, indent: int = 0) -> None:
        for key, value in stats.items():
            if isinstance(value, dict):
                self.print("  " * indent + f"{key}:")
                self._print_stats(value, indent + 1)
            else:
                self.print("  " * indent + f"{key}: {value}")

    # ------------------------------------------------------------------ REPL
    def repl(self, stdin: TextIO | None = None) -> None:
        stdin = stdin or sys.stdin
        interactive = stdin.isatty()
        if interactive:
            try:  # line editing / history where available (not on plain Windows consoles)
                import readline  # noqa: F401
            except ImportError:
                pass
        self.print(f"PyDB {__version__}")
        self.print(f"Connected to {self.db.engine.path}")
        self.print('Enter ".help" for usage hints.')
        self.print()
        buffer = ""
        while True:
            prompt = "pydb> " if not buffer else "....> "
            try:
                if interactive:
                    line = input(prompt)
                else:
                    line = stdin.readline()
                    if not line:
                        break
                    line = line.rstrip("\n")
                    self.out.write(prompt + line + "\n")
            except EOFError:
                break
            except KeyboardInterrupt:
                self.print("^C")
                buffer = ""
                continue
            if not buffer and line.strip().startswith("."):
                if not self.run_command(line):
                    break
                continue
            buffer += line + "\n"
            if statement_complete(buffer):
                self.print()
                self.run_sql(buffer)
                buffer = ""
        if buffer.strip():
            self.run_sql(buffer)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pydb", description="PyDB interactive SQL shell")
    parser.add_argument("database", nargs="?", default=DEFAULT_DB, help=f"database file (default: {DEFAULT_DB})")
    parser.add_argument("--debug", action="store_true", help="verbose logging and full tracebacks")
    parser.add_argument("-c", "--command", help="execute SQL and exit")
    parser.add_argument("--version", action="version", version=f"PyDB {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        db = Database(args.database)
    except DatabaseError as exc:
        print(f"Error: cannot open {args.database}: {exc}", file=sys.stderr)
        return 1
    if db.engine.last_recovery.ran:
        print(db.engine.last_recovery.summary())
    shell = Shell(db, debug=args.debug)
    try:
        if args.command:
            shell.run_sql(args.command)
        else:
            shell.repl()
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
