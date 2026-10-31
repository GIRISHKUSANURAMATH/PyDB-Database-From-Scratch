"""End-to-end verification of a PyDB checkout on the current machine.

    python scripts/windows_verify.py

Runs real processes, not just unit tests: an interactive CLI session, persistence
across two processes, B+ tree splits, transactions/isolation, a hard process
crash followed by recovery, and every example program. Prints PASS/FAIL per
check and exits non-zero if anything failed. Works on Windows, Linux and macOS.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydb import Database  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name):
    def deco(fn):
        def run(workdir):
            try:
                detail = fn(workdir) or ""
                RESULTS.append((name, True, detail))
                print(f"PASS  {name}  {detail}", flush=True)
            except Exception as exc:  # report and keep going
                RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
                print(f"FAIL  {name}\n{traceback.format_exc()}", flush=True)
        run.__name__ = fn.__name__
        return run
    return deco


def py(args, **kw):
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=300, cwd=ROOT, **kw)


CLI_SCRIPT = """\
CREATE TABLE users (
id INTEGER PRIMARY KEY,
name TEXT,
age INTEGER
);
INSERT INTO users VALUES (1, 'Girish', 21);
INSERT INTO users VALUES (2, 'Asha', 34), (3, 'Ravi', 17);
SELECT * FROM users;
UPDATE users SET age = 22 WHERE id = 1;
DELETE FROM users WHERE id = 3;
CREATE INDEX idx_users_age ON users(age);
SELECT name FROM users WHERE age = 22;
EXPLAIN SELECT name FROM users WHERE age = 22;
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), amount FLOAT);
INSERT INTO orders VALUES (10, 1, 250.0), (11, 2, 99.5);
SELECT users.name, orders.amount FROM users INNER JOIN orders ON users.id = orders.user_id ORDER BY orders.id;
SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY age;
BEGIN;
INSERT INTO users VALUES (9, 'Temp', 50);
ROLLBACK;
SELECT COUNT(*) AS temp_rows FROM users WHERE id = 9;
BEGIN;
INSERT INTO users VALUES (4, 'Meena', 21);
COMMIT;
SELECT * FROM users ORDER BY age DESC LIMIT 2;
INSERT INTO users VALUES (1, 'dup', 1);
.tables
.indexes
.stats
.quit
"""


@check("CLI session (python -m pydb)")
def cli_session(work):
    db = os.path.join(work, "cli.db")
    proc = py(["-m", "pydb", db], input=CLI_SCRIPT)
    out = proc.stdout
    assert proc.returncode == 0, proc.stderr
    for expected in ["PyDB 0.1.0", "Table created.", "3 rows returned.", "1 row updated.", "1 row deleted.",
                     "Index created.", "IndexScan(users, idx_users_age: age = 22)", "| Girish | 250.0  |",
                     "Transaction rolled back.", "Transaction committed.", "| 0         |",
                     "Error: PRIMARY KEY constraint failed", "buffer_pool:"]:
        assert expected in out, f"missing {expected!r} in CLI output:\n{out}"
    with open(os.path.join(ROOT, "data", "windows_cli_session.txt"), "w", encoding="utf-8") as fh:
        fh.write(out)
    return "transcript saved to data/windows_cli_session.txt"


@check("persistence across two processes")
def persistence(work):
    path = os.path.join(work, "persist.db")
    writer = f"""
import sys; sys.path.insert(0, {ROOT!r})
from pydb import Database
db = Database({path!r})
db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
with db.transaction():
    for i in range(1000):
        db.execute("INSERT INTO t VALUES (?, ?)", (i, "value-%d" % i))
db.close()
"""
    reader = f"""
import sys; sys.path.insert(0, {ROOT!r})
from pydb import Database
db = Database({path!r})
print(db.execute("SELECT COUNT(*) FROM t").scalar(), db.execute("SELECT v FROM t WHERE id = 777").scalar())
print(db.engine.last_recovery.ran)
db.close()
"""
    a = py(["-c", writer])
    assert a.returncode == 0, a.stderr
    b = py(["-c", reader])
    assert b.returncode == 0, b.stderr
    assert b.stdout.split() == ["1000", "value-777", "False"], b.stdout
    return "1000 rows written by process 1, read by process 2, clean shutdown"


@check("B+ tree splits, range scans and persistence")
def btree(work):
    path = os.path.join(work, "bt.db")
    db = Database(path, btree_max_entries=4, buffer_pool_size=16)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, grp INTEGER)")
    db.execute("CREATE INDEX t_grp ON t(grp)")
    with db.transaction():
        for i in range(3000):
            db.execute("INSERT INTO t VALUES (?, ?)", ((i * 7919) % 3000, i % 50))
    stats = {ix["name"]: ix for ix in db.indexes()}
    assert stats["pk_t"]["height"] >= 5, stats
    assert "IndexScan" in db.explain("SELECT * FROM t WHERE id BETWEEN 10 AND 20")
    assert [r[0] for r in db.execute("SELECT id FROM t WHERE id BETWEEN 10 AND 20 ORDER BY id")] == list(range(10, 21))
    db.close()
    db = Database(path, btree_max_entries=4)
    for info in db.engine.catalog.indexes.values():
        db.engine.indexes.tree(info).check_invariants()
    assert db.execute("SELECT COUNT(*) FROM t WHERE grp = 7").scalar() == 60
    h = stats["pk_t"]["height"]
    db.close()
    return f"3000 keys, pk height {h}, invariants hold after reopen"


@check("transactions: commit, rollback, isolation, deadlock")
def transactions(work):
    import threading
    import time

    from pydb.errors import DeadlockError, LockTimeoutError

    db = Database(os.path.join(work, "tx.db"), lock_timeout=0.5)
    db.execute("CREATE TABLE a (id INTEGER PRIMARY KEY)")
    db.execute("CREATE TABLE b (id INTEGER PRIMARY KEY)")
    db.execute("BEGIN")
    db.execute("INSERT INTO a VALUES (1)")
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM a").scalar() == 0
    w, r = db.session(), db.session()
    w.execute("BEGIN")
    w.execute("INSERT INTO a VALUES (2)")
    try:
        r.execute("SELECT * FROM a")
        raise AssertionError("dirty read possible")
    except LockTimeoutError:
        pass
    w.execute("COMMIT")
    assert r.execute("SELECT COUNT(*) FROM a").scalar() == 1
    db.engine.locks.timeout = 10
    s1, s2 = db.session(), db.session()
    s1.execute("BEGIN")
    s2.execute("BEGIN")
    s1.execute("INSERT INTO a VALUES (3)")
    s2.execute("INSERT INTO b VALUES (3)")
    th = threading.Thread(target=lambda: (s1.execute("INSERT INTO b VALUES (4)"), s1.execute("COMMIT")))
    th.start()
    deadline = time.monotonic() + 10
    while s1.txn.txn_id not in db.engine.locks._waits_for and time.monotonic() < deadline:
        time.sleep(0.01)
    try:
        s2.execute("INSERT INTO a VALUES (4)")
        raise AssertionError("deadlock not detected")
    except DeadlockError:
        pass
    th.join(10)
    assert db.execute("SELECT COUNT(*) FROM b").scalar() == 1
    db.close()
    return "rollback undone, reader blocked by writer, deadlock victim chosen"


@check("crash recovery after a hard process kill")
def crash(work):
    path = os.path.join(work, "crash.db")
    child = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {ROOT!r})
        from pydb import Database
        db = Database({path!r}, buffer_pool_size=8)
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        db.execute("CREATE INDEX t_v ON t(v)")
        with db.transaction():
            for i in range(300):
                db.execute("INSERT INTO t VALUES (?, ?)", (i, "ok-%d" % (i % 5)))
        db.execute("BEGIN")
        for i in range(300, 700):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "in-flight" + "!" * 150))
        db.execute("DELETE FROM t WHERE id < 50")
        os._exit(1)
    """)
    proc = py(["-c", child])
    assert proc.returncode == 1, proc.stderr
    db = Database(path)
    rep = db.engine.last_recovery
    assert rep.ran and rep.losers, rep.summary()
    ids = [r[0] for r in db.execute("SELECT id FROM t ORDER BY id")]
    assert ids == list(range(300)), f"{len(ids)} rows"
    assert db.execute("SELECT COUNT(*) FROM t WHERE v = 'ok-3'").scalar() == 60
    assert "IndexScan" in db.explain("SELECT * FROM t WHERE v = 'ok-3'")
    summary = rep.summary()
    db.close()
    return summary


@check("examples run")
def examples(work):
    names = ["quickstart.py", "social_network.py", "crash_recovery_demo.py"]
    for name in names:
        proc = py([os.path.join("examples", name)])
        assert proc.returncode == 0, f"{name}: {proc.stderr}"
    out = py([os.path.join("examples", "crash_recovery_demo.py")]).stdout
    assert "rows after recovery: 100 (expected 100)" in out, out
    return ", ".join(names)


@check("inspection scripts run")
def scripts(work):
    path = os.path.join(work, "insp.db")
    with Database(path) as db:
        db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO t VALUES (1)")
    for script, args in [("inspect_db.py", [path]), ("dump_page.py", [path, "4"]),
                         ("dump_wal.py", [path]), ("dump_btree.py", [path, "pk_t"])]:
        proc = py([os.path.join("scripts", script), *args])
        assert proc.returncode == 0, f"{script}: {proc.stderr}"
    return "inspect_db, dump_page, dump_wal, dump_btree"


def main() -> int:
    print(f"PyDB end-to-end verification on {platform.system()} {platform.release()} "
          f"({platform.machine()}), Python {platform.python_version()} at {sys.executable}")
    work = tempfile.mkdtemp(prefix="pydb-verify-")
    try:
        for fn in [cli_session, persistence, btree, transactions, crash, examples, scripts]:
            fn(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\nSUMMARY: {len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
