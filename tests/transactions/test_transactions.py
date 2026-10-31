"""Transactions, constraints, isolation and concurrency."""

from __future__ import annotations

import threading
import time

import pytest

from pydb import Database
from pydb.errors import CatalogError, ConstraintViolationError, DeadlockError, LockTimeoutError, TransactionError
from pydb.transaction.lock_manager import LockManager, LockMode


def count(db, table="t"):
    return db.execute(f"SELECT COUNT(*) FROM {table}").scalar()


@pytest.fixture
def tdb(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    return db


# ----------------------------------------------------------------------------- basic semantics
def test_commit_makes_changes_visible(tdb):
    tdb.execute("BEGIN")
    tdb.execute("INSERT INTO t VALUES (1, 'a')")
    tdb.execute("INSERT INTO t VALUES (2, 'b')")
    tdb.execute("COMMIT")
    assert count(tdb) == 2


def test_rollback_insert(tdb):
    tdb.execute("BEGIN")
    tdb.execute("INSERT INTO t VALUES (1, 'a')")
    assert count(tdb) == 1  # own uncommitted change is visible to the transaction
    tdb.execute("ROLLBACK")
    assert count(tdb) == 0
    assert tdb.execute("SELECT * FROM t WHERE id = 1").rows == []  # index entry undone too


def test_rollback_update_and_delete(tdb):
    tdb.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (3, 'c')")
    tdb.execute("BEGIN")
    tdb.execute("UPDATE t SET v = 'changed' WHERE id = 1")
    tdb.execute("UPDATE t SET v = ? WHERE id = 2", ("x" * 3000,))  # moved to another page
    tdb.execute("DELETE FROM t WHERE id = 3")
    tdb.execute("INSERT INTO t VALUES (4, 'd')")
    tdb.execute("ROLLBACK")
    assert tdb.execute("SELECT * FROM t ORDER BY id").rows == [(1, "a"), (2, "b"), (3, "c")]
    assert tdb.execute("SELECT v FROM t WHERE id = 2").rows == [("b",)]
    assert tdb.execute("SELECT v FROM t WHERE id = 4").rows == []


def test_rollback_restores_primary_key_slot(tdb):
    tdb.execute("INSERT INTO t VALUES (1, 'a')")
    tdb.execute("BEGIN")
    tdb.execute("DELETE FROM t WHERE id = 1")
    tdb.execute("INSERT INTO t VALUES (1, 'replacement')")
    tdb.execute("ROLLBACK")
    assert tdb.execute("SELECT v FROM t WHERE id = 1").rows == [("a",)]
    with pytest.raises(ConstraintViolationError):
        tdb.execute("INSERT INTO t VALUES (1, 'dup')")


def test_many_statements_rollback(tdb):
    with pytest.raises(RuntimeError):
        with tdb.transaction():
            for i in range(300):
                tdb.execute("INSERT INTO t VALUES (?, ?)", (i, f"row {i}"))
            raise RuntimeError("boom")
    assert count(tdb) == 0
    with tdb.transaction():
        for i in range(300):
            tdb.execute("INSERT INTO t VALUES (?, ?)", (i, f"row {i}"))
    assert count(tdb) == 300


def test_statement_atomicity_inside_transaction(tdb):
    tdb.execute("INSERT INTO t VALUES (5, 'e')")
    tdb.execute("BEGIN")
    tdb.execute("INSERT INTO t VALUES (1, 'a')")
    with pytest.raises(ConstraintViolationError):
        tdb.execute("INSERT INTO t VALUES (2, 'b'), (3, 'c'), (5, 'dup')")  # fails on 3rd row
    assert tdb.in_transaction  # the transaction survives a failed statement
    tdb.execute("COMMIT")
    assert [r[0] for r in tdb.execute("SELECT id FROM t ORDER BY id")] == [1, 5]


def test_failed_autocommit_statement_is_atomic(tdb):
    with pytest.raises(ConstraintViolationError):
        tdb.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b'), (1, 'dup')")
    assert count(tdb) == 0


def test_transaction_errors(tdb):
    with pytest.raises(TransactionError):
        tdb.execute("COMMIT")
    with pytest.raises(TransactionError):
        tdb.execute("ROLLBACK")
    tdb.execute("BEGIN")
    with pytest.raises(TransactionError):
        tdb.execute("BEGIN")
    with pytest.raises(TransactionError):
        tdb.execute("CREATE TABLE x (a INTEGER)")
    tdb.execute("ROLLBACK")


def test_close_rolls_back_open_transaction(db_path):
    db = Database(db_path)
    db.execute("CREATE TABLE t (id INTEGER)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    db.close()
    db = Database(db_path)
    assert count(db) == 0
    db.close()


# ----------------------------------------------------------------------------- constraints
def test_primary_key(db):
    db.execute("CREATE TABLE p (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO p VALUES (1, 'a')")
    with pytest.raises(ConstraintViolationError, match="PRIMARY KEY"):
        db.execute("INSERT INTO p VALUES (1, 'b')")
    with pytest.raises(ConstraintViolationError, match="PRIMARY KEY"):
        db.execute("INSERT INTO p VALUES (NULL, 'b')")
    db.execute("INSERT INTO p VALUES (2, 'b')")
    with pytest.raises(ConstraintViolationError):
        db.execute("UPDATE p SET id = 1 WHERE id = 2")
    db.execute("UPDATE p SET name = 'same key ok' WHERE id = 2")


def test_table_level_primary_key(db):
    db.execute("CREATE TABLE p (id INTEGER, name TEXT, PRIMARY KEY (id))")
    db.execute("INSERT INTO p VALUES (1, 'a')")
    with pytest.raises(ConstraintViolationError):
        db.execute("INSERT INTO p VALUES (1, 'b')")


def test_unique_and_not_null(db):
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, nick TEXT UNIQUE)")
    db.execute("INSERT INTO u VALUES (1, 'a@x', NULL)")
    db.execute("INSERT INTO u VALUES (2, 'b@x', NULL)")  # many NULLs allowed in UNIQUE
    with pytest.raises(ConstraintViolationError, match="UNIQUE"):
        db.execute("INSERT INTO u VALUES (3, 'a@x', 'n')")
    with pytest.raises(ConstraintViolationError, match="NOT NULL"):
        db.execute("INSERT INTO u (id) VALUES (4)")
    with pytest.raises(ConstraintViolationError):
        db.execute("UPDATE u SET email = NULL WHERE id = 1")


def test_default(db):
    db.execute("CREATE TABLE d (id INTEGER, active BOOLEAN DEFAULT TRUE, n INTEGER DEFAULT -1)")
    db.execute("INSERT INTO d (id) VALUES (1)")
    assert db.execute("SELECT * FROM d").rows == [(1, True, -1)]


def test_create_unique_index_on_duplicates_fails(db):
    db.execute("CREATE TABLE x (a INTEGER)")
    db.execute("INSERT INTO x VALUES (1), (1)")
    with pytest.raises(ConstraintViolationError):
        db.execute("CREATE UNIQUE INDEX ix ON x(a)")
    assert db.indexes() == []
    db.execute("CREATE INDEX ix ON x(a)")
    assert db.execute("SELECT COUNT(*) FROM x WHERE a = 1").scalar() == 2


def test_foreign_keys(db):
    db.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))")
    db.execute("INSERT INTO parent VALUES (1)")
    db.execute("INSERT INTO child VALUES (10, 1)")
    db.execute("INSERT INTO child VALUES (11, NULL)")
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        db.execute("INSERT INTO child VALUES (12, 99)")
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        db.execute("DELETE FROM parent WHERE id = 1")
    with pytest.raises(ConstraintViolationError, match="FOREIGN KEY"):
        db.execute("UPDATE parent SET id = 2 WHERE id = 1")
    with pytest.raises(ConstraintViolationError):
        db.execute("DROP TABLE parent")
    db.execute("DELETE FROM child WHERE id = 10")
    db.execute("DELETE FROM parent WHERE id = 1")
    with pytest.raises(CatalogError):
        db.execute("CREATE TABLE bad (x TEXT REFERENCES parent(id))")  # type mismatch


# ----------------------------------------------------------------------------- isolation / concurrency
def test_no_dirty_reads_between_sessions(db):
    db.execute("CREATE TABLE t (id INTEGER)")
    db.engine.config.lock_timeout = 0.3
    db.engine.locks.timeout = 0.3
    writer, reader = db.session(), db.session()
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (1)")
    with pytest.raises(LockTimeoutError):  # reader must wait for writer's X lock
        reader.execute("SELECT * FROM t")
    writer.execute("COMMIT")
    assert reader.execute("SELECT COUNT(*) FROM t").scalar() == 1


def test_reader_blocks_until_writer_commits(db):
    db.execute("CREATE TABLE t (id INTEGER)")
    writer, reader = db.session(), db.session()
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t VALUES (1)")
    seen = []

    def read():
        seen.append(reader.execute("SELECT COUNT(*) FROM t").scalar())

    th = threading.Thread(target=read)
    th.start()
    time.sleep(0.2)
    assert seen == []  # still blocked behind the writer's X lock
    writer.execute("INSERT INTO t VALUES (2)")
    writer.execute("COMMIT")
    th.join(5)
    assert seen == [2]  # saw the committed state, never a partial one


def test_shared_locks_allow_concurrent_readers(db):
    db.execute("CREATE TABLE t (id INTEGER)")
    a, b = db.session(), db.session()
    a.execute("BEGIN")
    b.execute("BEGIN")
    a.execute("SELECT * FROM t")
    b.execute("SELECT * FROM t")  # S + S compatible, no wait
    a.execute("COMMIT")
    b.execute("COMMIT")


def test_deadlock_detection(db):
    db.execute("CREATE TABLE a (id INTEGER)")
    db.execute("CREATE TABLE b (id INTEGER)")
    s1, s2 = db.session(), db.session()
    s1.execute("BEGIN")
    s2.execute("BEGIN")
    s1.execute("INSERT INTO a VALUES (1)")  # s1: X(a)
    s2.execute("INSERT INTO b VALUES (1)")  # s2: X(b)
    errors = []

    def t1():
        try:
            s1.execute("INSERT INTO b VALUES (2)")  # waits for s2
            s1.execute("COMMIT")
        except DeadlockError as e:  # pragma: no cover - depends on timing
            errors.append(("s1", e))

    th = threading.Thread(target=t1)
    th.start()
    deadline = time.monotonic() + 10  # wait until s1 is really blocked (robust on slow machines)
    while s1.txn.txn_id not in db.engine.locks._waits_for and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        s2.execute("INSERT INTO a VALUES (2)")  # closes the cycle -> DeadlockError
        s2.execute("COMMIT")
    except DeadlockError as e:
        errors.append(("s2", e))
    th.join(5)
    assert len(errors) == 1
    assert errors[0][0] == "s2"
    assert not s2.in_transaction  # the victim was rolled back
    assert db.execute("SELECT COUNT(*) FROM b").scalar() == 1  # s1's row; s2's insert was rolled back
    assert db.execute("SELECT COUNT(*) FROM a").scalar() == 1


def test_concurrent_transfers_preserve_total(db):
    """Serializable bank transfers from many threads: money is never created or lost."""
    db.execute("CREATE TABLE acct (id INTEGER PRIMARY KEY, balance INTEGER)")
    with db.transaction():
        for i in range(10):
            db.execute("INSERT INTO acct VALUES (?, 1000)", (i,))

    deadlocks: list[int] = []

    def worker(seed: int) -> None:
        s = db.session()
        for k in range(25):
            src, dst = (seed + k) % 10, (seed * 3 + k + 1) % 10
            if src == dst:
                continue
            while True:  # read-then-write upgrades S->X, so two workers can deadlock: retry
                try:
                    s.execute("BEGIN")
                    bal = s.execute("SELECT balance FROM acct WHERE id = ?", (src,)).scalar()
                    s.execute("UPDATE acct SET balance = ? WHERE id = ?", (bal - 7, src))
                    s.execute("UPDATE acct SET balance = balance + 7 WHERE id = ?", (dst,))
                    s.execute("COMMIT")
                    break
                except DeadlockError:
                    deadlocks.append(seed)  # the victim's transaction was already rolled back

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert db.execute("SELECT SUM(balance) FROM acct").scalar() == 10_000


def test_lock_manager_upgrade_and_compatibility():
    lm = LockManager(timeout=0.2)
    lm.acquire(1, "r", LockMode.SHARED)
    lm.acquire(2, "r", LockMode.SHARED)
    with pytest.raises(LockTimeoutError):
        lm.acquire(1, "r", LockMode.EXCLUSIVE)  # cannot upgrade while T2 shares it
    lm.release_all(2)
    lm.acquire(1, "r", LockMode.EXCLUSIVE)  # sole holder: upgrade succeeds
    assert lm.holders("r") == {1: LockMode.EXCLUSIVE}
    with pytest.raises(LockTimeoutError):
        lm.acquire(3, "r", LockMode.SHARED)
    lm.release_all(1)
    lm.acquire(3, "r", LockMode.SHARED)
