"""Crash-recovery tests.

Two crash styles are used:

* ``db._simulate_crash()`` — in-process: the buffer pool and any un-flushed
  WAL records are thrown away and files are closed without syncing. Pages the
  buffer pool had already evicted *do* stay on disk, exactly like a real crash.
* a real child process killed with ``os._exit`` in the middle of its work.
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import textwrap

from pydb import Database
from pydb.transaction.wal import LogRecord, LogType, WriteAheadLog

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def ids(db, table="t"):
    return [r[0] for r in db.execute(f"SELECT id FROM {table} ORDER BY id")]


def check_indexes(db):
    """Every index must contain exactly the table's rows and be structurally valid."""
    for info in db.engine.catalog.indexes.values():
        schema = db.engine.catalog.table_by_id(info.table_id)
        tree = db.engine.indexes.tree(info)
        tree.check_invariants()
        pos = schema.position(info.column)
        expected = sorted((row[pos], *rid) for rid, row in db.engine.table(schema).scan() if row[pos] is not None)
        assert sorted(tree) == expected, f"index {info.name} out of sync"


def make(path, **kw):
    db = Database(path, **kw)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    return db


# ----------------------------------------------------------------------------- redo / undo
def test_committed_data_survives_crash_via_redo(db_path):
    db = make(db_path)
    for i in range(50):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, f"v{i}"))
    assert db.engine.disk.writes < 10  # nothing much has reached the data file yet
    db._simulate_crash()

    db = Database(db_path)
    report = db.engine.last_recovery
    assert report.ran and report.redone > 0
    assert ids(db) == list(range(50))
    assert db.execute("SELECT v FROM t WHERE id = 42").rows == [("v42",)]
    check_indexes(db)
    db.close()


def test_uncommitted_transaction_is_rolled_back(db_path):
    db = make(db_path)
    db.execute("INSERT INTO t VALUES (1, 'committed')")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (2, 'uncommitted')")
    db.execute("UPDATE t SET v = 'dirty' WHERE id = 1")
    db.engine.wal.flush()  # the loser's records reach the log, but no COMMIT
    db._simulate_crash()

    db = Database(db_path)
    assert db.engine.last_recovery.losers
    assert db.execute("SELECT * FROM t").rows == [(1, "committed")]
    check_indexes(db)
    db.close()


def test_steal_uncommitted_pages_on_disk_are_undone(db_path):
    """With a tiny buffer pool, uncommitted pages are evicted (stolen) to disk mid-transaction.
    Recovery must use the WAL before-images to undo them."""
    db = make(db_path, buffer_pool_size=8)
    with db.transaction():
        for i in range(200):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "base" + "x" * 50))
    db.checkpoint()
    db.execute("BEGIN")
    for i in range(200, 600):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "loser" + "y" * 50))
    db.execute("UPDATE t SET v = 'overwritten' WHERE id < 100")
    db.execute("DELETE FROM t WHERE id >= 150 AND id < 200")
    assert db.engine.pool.stats.dirty_flushes > 0  # stolen pages hit the disk
    db._simulate_crash()

    db = Database(db_path)
    report = db.engine.last_recovery
    assert report.undone > 0
    assert ids(db) == list(range(200))
    assert db.execute("SELECT COUNT(*) FROM t WHERE v = 'overwritten'").scalar() == 0
    check_indexes(db)
    db.close()


def test_rollback_then_slot_reuse_then_crash(db_path):
    """CLRs make a rolled-back transaction 'complete' — recovery must not undo it again,
    or it would destroy the later committed row that reused the same slot."""
    db = make(db_path)
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1, 'rolled back')")
    db.execute("ROLLBACK")
    db.execute("INSERT INTO t VALUES (2, 'reuses the slot')")
    db._simulate_crash()

    db = Database(db_path)
    assert db.engine.last_recovery.aborted
    assert db.execute("SELECT * FROM t").rows == [(2, "reuses the slot")]
    db.close()


def test_crash_after_ddl(db_path):
    db = Database(db_path)
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    db.execute("CREATE INDEX a_name ON a(name)")
    db.execute("INSERT INTO a VALUES (1, 'x'), (2, 'y')")
    db.execute("CREATE TABLE gone (id INTEGER)")
    db.execute("INSERT INTO gone VALUES (1)")
    db.execute("DROP TABLE gone")
    db._simulate_crash()

    db = Database(db_path)
    assert db.tables() == ["a"]
    assert {ix["name"] for ix in db.indexes()} == {"pk_a", "uq_a_name", "a_name"}
    assert db.execute("SELECT id FROM a WHERE name = 'y'").rows == [(2,)]
    assert "IndexScan" in db.explain("SELECT id FROM a WHERE name = 'y'")
    check_indexes(db)
    db.close()


def test_torn_wal_tail_is_ignored(db_path):
    db = make(db_path)
    db.execute("INSERT INTO t VALUES (1, 'safe')")
    db._simulate_crash()
    with open(db_path + ".wal", "ab") as fh:  # half-written record at the end of the log
        fh.write(LogRecord(LogType.INSERT, 99, owner=1, page_id=1, slot=0, after=b"junk").encode()[:13])

    db = Database(db_path)
    assert db.execute("SELECT * FROM t").rows == [(1, "safe")]
    db.close()


def test_corrupted_wal_record_stops_replay(db_path):
    db = make(db_path)
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db.execute("INSERT INTO t VALUES (2, 'b')")
    db._simulate_crash()
    wal = WriteAheadLog(db_path + ".wal")
    records = wal.records()
    wal.close()
    commit_lsns = [r.lsn for r in records if r.type is LogType.COMMIT]
    assert len(commit_lsns) >= 2
    # Flip a byte in the final COMMIT record -> the second insert is no longer committed.
    with open(db_path + ".wal", "r+b") as fh:
        fh.seek(-3, os.SEEK_END)
        b = fh.read(1)
        fh.seek(-3, os.SEEK_END)
        fh.write(bytes([b[0] ^ 0xFF]))
    db = Database(db_path)
    assert ids(db) == [1]
    db.close()


def test_crash_during_recovery_is_safe(db_path, monkeypatch):
    db = make(db_path, buffer_pool_size=8)
    with db.transaction():
        for i in range(100):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "c" * 60))
    db.execute("BEGIN")
    for i in range(100, 300):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "l" * 60))
    db._simulate_crash()

    # First recovery attempt "crashes" right before its final checkpoint, after
    # redo/undo have dirtied (and partly flushed) pages.
    from pydb import engine as engine_mod

    def boom(self):
        self.pool.flush_all()  # some recovered state reaches disk...
        raise KeyboardInterrupt("power cut during recovery")

    monkeypatch.setattr(engine_mod.Engine, "_rebuild_indexes_after_recovery", boom)
    try:
        Database(db_path, buffer_pool_size=8)
    except KeyboardInterrupt:
        pass
    monkeypatch.undo()

    db = Database(db_path)  # ...and a second recovery still converges
    assert ids(db) == list(range(100))
    check_indexes(db)
    db.close()


def test_recovery_is_idempotent_across_repeated_crashes(db_path):
    db = make(db_path)
    db.execute("INSERT INTO t VALUES (1, 'a')")
    db._simulate_crash()
    for _ in range(3):
        db = Database(db_path)
        db.execute("INSERT INTO t VALUES (?, 'more')", (len(ids(db)) + 1,))
        db._simulate_crash()
    db = Database(db_path)
    assert ids(db) == [1, 2, 3, 4]
    db.close()


def test_clean_close_empties_the_wal(db_path):
    db = make(db_path)
    db.execute("INSERT INTO t VALUES (1, 'a')")
    assert db.engine.wal.size() > 16
    db.close()
    assert os.path.getsize(db_path + ".wal") == 16  # just the header
    db = Database(db_path)
    assert not db.engine.last_recovery.ran
    db.close()


# ----------------------------------------------------------------------------- real process crash
CHILD = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, {root!r})
    from pydb import Database
    db = Database({path!r}, buffer_pool_size=16)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    db.execute("CREATE INDEX t_v ON t(v)")
    with db.transaction():
        for i in range(300):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "committed-%d" % (i % 7)))
    db.execute("BEGIN")
    for i in range(300, 700):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "in-flight"))
    db.execute("DELETE FROM t WHERE id < 50")
    print("about to crash", flush=True)
    os._exit(1)   # hard kill: no close(), no checkpoint, no flush
    """
)


def test_real_process_crash(tmp_path):
    path = str(tmp_path / "proc.db")
    script = tmp_path / "child.py"
    script.write_text(CHILD.format(root=ROOT, path=path))
    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stderr
    assert "about to crash" in proc.stdout

    db = Database(path)
    assert db.engine.last_recovery.ran
    assert ids(db) == list(range(300))
    assert db.execute("SELECT COUNT(*) FROM t WHERE v = 'in-flight'").scalar() == 0
    assert db.execute("SELECT COUNT(*) FROM t WHERE v = 'committed-3'").scalar() == len(
        [i for i in range(300) if i % 7 == 3]
    )
    check_indexes(db)
    db.close()


def test_persistence_across_processes(tmp_path):
    path = str(tmp_path / "persist.db")
    writer = f"""
import sys; sys.path.insert(0, {ROOT!r})
from pydb import Database
db = Database({path!r})
db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
db.execute("INSERT INTO users VALUES (1, 'Girish', 21), (2, 'Asha', 30)")
db.close()
"""
    reader = f"""
import sys; sys.path.insert(0, {ROOT!r})
from pydb import Database
db = Database({path!r})
print(db.execute("SELECT name FROM users WHERE age >= 18 ORDER BY id").rows)
db.close()
"""
    subprocess.run([sys.executable, "-c", writer], check=True, timeout=60)
    out = subprocess.run([sys.executable, "-c", reader], check=True, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "[('Girish',), ('Asha',)]"


# ----------------------------------------------------------------------------- randomized crash testing
def test_randomized_crashes_match_committed_model(db_path):
    rng = random.Random(2024)
    committed: dict[int, str] = {}
    db = make(db_path, buffer_pool_size=8)
    for _round in range(12):
        working = dict(committed)
        db.execute("BEGIN")
        for _ in range(rng.randint(5, 60)):
            op = rng.random()
            if op < 0.5:
                k = rng.randint(0, 400)
                if k not in working:
                    v = "v" * rng.randint(1, 300)
                    db.execute("INSERT INTO t VALUES (?, ?)", (k, v))
                    working[k] = v
            elif op < 0.8 and working:
                k = rng.choice(list(working))
                v = "u" * rng.randint(1, 900)
                db.execute("UPDATE t SET v = ? WHERE id = ?", (v, k))
                working[k] = v
            elif working:
                k = rng.choice(list(working))
                db.execute("DELETE FROM t WHERE id = ?", (k,))
                del working[k]
        outcome = rng.random()
        if outcome < 0.5:
            db.execute("COMMIT")
            committed = working
        elif outcome < 0.7:
            db.execute("ROLLBACK")
        if outcome >= 0.7 or rng.random() < 0.5:  # crash, with or without an open transaction
            if outcome >= 0.7:
                db.engine.wal.flush()
            db._simulate_crash()
            db = Database(db_path, buffer_pool_size=8)
            if outcome >= 0.7:
                assert db.engine.last_recovery.losers
        assert dict(db.execute("SELECT id, v FROM t").rows) == committed
    check_indexes(db)
    db.close()
