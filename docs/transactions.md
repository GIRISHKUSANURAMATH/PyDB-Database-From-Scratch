# Transactions and concurrency

Source: `pydb/transaction/transaction.py`, `pydb/transaction/lock_manager.py`,
`pydb/execution/executor.py`.

## Transaction model

* `BEGIN` … `COMMIT` / `ROLLBACK` — explicit transaction on the current session.
* Outside an explicit transaction every statement is its own **autocommit** transaction.
* `with db.transaction():` commits on success and rolls back on an exception.
* **Statement atomicity:** before each statement the session records a savepoint (the
  length of the transaction's undo list). If the statement fails — e.g. the third row of
  a multi-row INSERT violates a PRIMARY KEY — only that statement's changes are undone and
  the transaction stays open.
* DDL (`CREATE`/`DROP`) is allowed only outside explicit transactions (it runs as its own
  transaction and takes an exclusive catalog lock).

## What COMMIT does

1. Append a `COMMIT` record to the WAL.
2. Flush the WAL and `fsync` it. Once `fsync` returns, the transaction is **durable**.
3. Run deferred actions (e.g. wipe the pages of a dropped table).
4. Release all locks.

Data pages are *not* written at commit (no-force); the log is enough to redo them.
Read-only transactions never write to the log at all (BEGIN is logged lazily on the
first change).

## What ROLLBACK does

For each logged change, newest first:

1. Write a **compensation log record (CLR)** describing the undo, e.g. a DELETE for an
   INSERT, an UPDATE back to the before-image for an UPDATE.
2. Apply it to the heap page.
3. Undo the change in every index (logical undo: delete the inserted key, re-insert the
   deleted key, …).

Finally an `ABORT` record is appended and the locks are released. The CLRs matter for
crash recovery: see [recovery.md](recovery.md#why-clrs).

## Concurrency control: strict two-phase locking

`LockManager` grants **shared (S)** and **exclusive (X)** locks on named resources:

| Statement | Locks |
|---|---|
| SELECT, EXPLAIN | S on `__catalog__`, S on every table read |
| INSERT / UPDATE / DELETE | S on `__catalog__`, X on the target table, S on tables related by foreign keys |
| CREATE / DROP | X on `__catalog__` |

* S is compatible with S; X is compatible with nothing.
* An S holder may upgrade to X when it is the only holder.
* **Strict** 2PL: locks are held until COMMIT or ROLLBACK.
* **Fairness:** a new S request queues behind a waiting X request, so a stream of readers
  cannot starve a writer. (This was added after the concurrent-transfer test showed a
  writer timing out while readers kept arriving.)
* **Deadlock detection:** before waiting, the manager walks the wait-for graph
  (holders and queued X-waiters of the resource → what they wait for → …). If the walk
  reaches the requester, it gets `DeadlockError` immediately and its transaction is rolled
  back. A timeout (`lock_timeout`, default 5 s) is the backstop.

A classic deadlock that PyDB detects: two transactions both `SELECT` from a table (both
hold S) and then both `UPDATE` it (both want X). Applications should retry the victim,
as the test `test_concurrent_transfers_preserve_total` does.

### Statement latch

Separately from locks, the engine has one re-entrant latch held while a statement
executes. Locks provide *isolation between transactions*; the latch protects the
in-memory structures (buffer pool, B+ tree nodes) while one statement runs. Locks are
always acquired **before** the latch, so a transaction waiting for a lock never blocks
others from running.

## Isolation level — stated precisely

PyDB provides **serializable** isolation for transactions in one process, at **table
granularity**:

* no dirty reads (a reader waits for the writer's X lock),
* no non-repeatable reads (an S lock is held until commit, so nobody can change the table),
* no phantoms (the whole table is locked, not individual rows),
* every schedule is conflict-serializable (strict 2PL).

The price is concurrency: two transactions writing different rows of the same table run
one after the other. PyDB does **not** implement MVCC, row-level locks, or snapshot
isolation, and it does not support several processes sharing one database file (a second
process gets `DatabaseLockedError`). Use `db.session()` to get one session per thread.

## Tests

`tests/transactions/test_transactions.py` covers commit, rollback of inserts / updates /
deletes / moved rows, statement atomicity, constraint enforcement, no-dirty-reads
between sessions, blocking until commit, shared readers, deadlock detection, lock
upgrades, and a multi-threaded bank-transfer test that checks the total balance is
preserved.
