"""Watch crash recovery happen.

    python examples/crash_recovery_demo.py

1. A child process commits 100 rows, starts another transaction that inserts
   200 more and deletes some committed ones, then kills itself with
   ``os._exit(1)`` — no close(), no checkpoint, buffer pool lost.
2. We inspect the files exactly as the crash left them.
3. We reopen the database: recovery replays the WAL (redo), rolls back the
   in-flight transaction (undo) and rebuilds the affected indexes.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydb import Database  # noqa: E402
from pydb.inspector import describe_wal  # noqa: E402

DB = os.path.join(ROOT, "data", "crash_demo.db")

CHILD = f"""
import os, sys
sys.path.insert(0, {ROOT!r})
from pydb import Database
db = Database({DB!r}, buffer_pool_size=8)   # tiny cache: dirty pages get evicted early
db.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, owner TEXT, balance INTEGER)")
with db.transaction():
    for i in range(100):
        db.execute("INSERT INTO accounts VALUES (?, ?, ?)", (i, "owner%d" % i, 1000))
db.execute("BEGIN")
for i in range(100, 300):
    db.execute("INSERT INTO accounts VALUES (?, ?, ?)", (i, "uncommitted" + "!" * 200, 5))
db.execute("DELETE FROM accounts WHERE id < 20")
print("child: 100 rows committed, 200 inserts + 20 deletes in flight ... crashing now", flush=True)
os._exit(1)
"""


def main() -> None:
    for suffix in ("", ".wal"):
        if os.path.exists(DB + suffix):
            os.remove(DB + suffix)
    subprocess.run([sys.executable, "-c", CHILD], check=False)

    wal = describe_wal(DB, limit=6)
    print("\nWAL left behind by the crashed process (first records):")
    print("\n".join("  " + line for line in wal.splitlines()))

    print("\nReopening the database ...")
    db = Database(DB)
    print(" ", db.engine.last_recovery.summary())
    total = db.execute("SELECT COUNT(*), SUM(balance) FROM accounts").fetchone()
    print(f"  rows after recovery: {total[0]} (expected 100), total balance: {total[1]} (expected 100000)")
    leftovers = db.execute("SELECT COUNT(*) FROM accounts WHERE owner LIKE 'uncommitted%'").scalar()
    print("  uncommitted rows present:", leftovers)
    print("  PK index lookup id=5:", db.execute("SELECT * FROM accounts WHERE id = 5").rows)
    db.close()


if __name__ == "__main__":
    main()
