"""CLI shell, lexer/parser details, WAL format and inspection tools."""

from __future__ import annotations

import io
import subprocess
import sys

import pytest

from pydb import Database
from pydb.cli import Shell, format_table, main, statement_complete
from pydb.errors import ParseError
from pydb.inspector import describe_database, describe_page, describe_wal
from pydb.sql import ast
from pydb.sql.lexer import TokenType, tokenize
from pydb.sql.parser import parse_one
from pydb.transaction.wal import FLAG_CLR, LogRecord, LogType, WriteAheadLog
from tests.recovery.test_recovery import ROOT


# ----------------------------------------------------------------------------- lexer / parser
def test_lexer_tokens():
    toks = tokenize("SELECT a, 'it''s', 3.5, 42, ? FROM t WHERE x <> 1 -- comment\n/* block */;")
    kinds = [t.type for t in toks]
    assert kinds[:4] == [TokenType.KEYWORD, TokenType.IDENT, TokenType.PUNCT, TokenType.STRING]
    assert toks[3].value == "it's"
    assert any(t.type is TokenType.OP and t.value == "!=" for t in toks)
    assert toks[-1].type is TokenType.EOF
    with pytest.raises(ParseError):
        tokenize("SELECT @")
    with pytest.raises(ParseError):
        tokenize("SELECT 12abc")


def test_parser_precedence():
    stmt, _ = parse_one("SELECT 1 + 2 * 3 = 7 OR NOT a AND b")
    expr = stmt.items[0].expr
    assert isinstance(expr, ast.BinaryOp) and expr.op == "OR"
    left = expr.left
    assert left.op == "=" and left.left.op == "+" and left.left.right.op == "*"
    assert expr.right.op == "AND" and isinstance(expr.right.left, ast.UnaryOp)


def test_parser_counts_parameters():
    _stmt, n = parse_one("SELECT * FROM t WHERE a = ? AND b IN (?, ?)")
    assert n == 3


def test_parser_quoted_identifier_and_keyword_column():
    stmt, _ = parse_one('CREATE TABLE "order" (key INTEGER, "select" TEXT)')
    assert stmt.name == "order" and [c.name for c in stmt.columns] == ["key", "select"]


# ----------------------------------------------------------------------------- WAL format
def test_wal_roundtrip_and_lsns(tmp_path):
    wal = WriteAheadLog(str(tmp_path / "x.wal"))
    recs = [
        LogRecord(LogType.BEGIN, 1),
        LogRecord(LogType.INSERT, 1, owner=100, page_id=4, slot=2, after=b"hello"),
        LogRecord(LogType.UPDATE, 1, owner=100, page_id=4, slot=2, before=b"hello", after=b"world!"),
        LogRecord(LogType.DELETE, 1, owner=100, page_id=4, slot=2, before=b"world!", flags=FLAG_CLR),
        LogRecord(LogType.NEW_PAGE, 1, owner=100, page_id=9, prev_page=4),
        LogRecord(LogType.COMMIT, 1),
    ]
    lsns = [wal.append(r) for r in recs]
    assert lsns == [1, 2, 3, 4, 5, 6]
    wal.close()
    wal = WriteAheadLog(str(tmp_path / "x.wal"))
    back = wal.records()
    assert [r.type for r in back] == [r.type for r in recs]
    assert back[2].before == b"hello" and back[2].after == b"world!"
    assert back[3].is_clr and back[4].prev_page == 4
    assert wal.next_lsn == 7
    wal.truncate()
    assert wal.records() == []
    assert wal.append(LogRecord(LogType.BEGIN, 2)) == 7  # LSNs never restart
    wal.close()


# ----------------------------------------------------------------------------- shell
def test_format_table():
    text = format_table(["id", "name"], [(1, "Girish"), (2, None), (3, True)])
    assert "| 1  | Girish |" in text and "NULL" in text and "TRUE" in text


def test_statement_complete():
    assert statement_complete("SELECT 1;")
    assert not statement_complete("SELECT 1")
    assert not statement_complete("SELECT ';")  # semicolon inside an unterminated string
    assert statement_complete("SELECT ';';")


def run_shell(db, script: str) -> str:
    out = io.StringIO()
    Shell(db, out=out).repl(io.StringIO(script))
    return out.getvalue()


def test_shell_session(db):
    out = run_shell(
        db,
        "CREATE TABLE users (\nid INTEGER PRIMARY KEY,\nname TEXT\n);\n"
        "INSERT INTO users VALUES (1, 'Girish');\n"
        "SELECT * FROM users;\n"
        "SELECT * FROM missing;\n"
        ".tables\n.schema users\n.indexes\n.explain SELECT * FROM users WHERE id = 1\n"
        ".stats\n.wal\n.checkpoint\n.btree pk_users\n.timer on\nSELECT 1;\n.bogus\n.quit\n",
    )
    assert "....> name TEXT" in out
    assert "Table created." in out and "1 row inserted." in out
    assert "| 1  | Girish |" in out and "1 row returned." in out
    assert "Error: no such table: missing" in out
    assert "IndexScan(users, pk_users: id = 1)" in out
    assert "buffer_pool:" in out and "Checkpoint complete." in out
    assert "Leaf[page" in out and "Run time:" in out and "Unknown command .bogus" in out


def test_cli_main_command_mode(tmp_path, capsys):
    path = str(tmp_path / "c.db")
    assert main([path, "-c", "CREATE TABLE t (a INTEGER); INSERT INTO t VALUES (7); SELECT * FROM t;"]) == 0
    assert "| 7 |" in capsys.readouterr().out


def test_python_dash_m_pydb(tmp_path):
    path = str(tmp_path / "m.db")
    proc = subprocess.run(
        [sys.executable, "-m", "pydb", path], input="SELECT 40 + 2 AS answer;\n.quit\n",
        capture_output=True, text=True, timeout=60, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert "PyDB 0.1.0" in proc.stdout and "| 42     |" in proc.stdout


# ----------------------------------------------------------------------------- inspection tools
def test_inspection_tools(db_path):
    db = Database(db_path)
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO users VALUES (1, 'Girish'), (2, 'Asha')")
    db.close()
    summary = describe_database(db_path)
    assert "Tables:            1" in summary and "users" in summary and "pk_users" in summary
    assert "clean shutdown" in summary
    page = describe_page(db_path, 4, show_bytes=True)
    assert "type:       HEAP" in page and "(1, 'Girish')" in page and "checksum" in page
    meta = describe_page(db_path, 5)
    assert "BTREE_META" in meta and "root page" in meta
    assert "0 records" in describe_wal(db_path)


def test_scripts_run(db_path):
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    db.close()
    for script, args in [("inspect_db.py", [db_path]), ("dump_page.py", [db_path, "1"]),
                         ("dump_wal.py", [db_path]), ("dump_btree.py", [db_path, "pk_t"])]:
        proc = subprocess.run([sys.executable, f"{ROOT}/scripts/{script}", *args],
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, (script, proc.stderr)
