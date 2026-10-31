# PyDB

**Database Management System Built From Scratch in Python**

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies: none](https://img.shields.io/badge/runtime%20dependencies-none-brightgreen)
![Platforms: Windows | Linux](https://img.shields.io/badge/tested%20on-Windows%2011%20%7C%20Linux-lightgrey)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

PyDB is a relational database engine written from scratch in pure Python, using only
the standard library. It has its own page-based storage engine, LRU buffer pool,
disk-based B+ tree indexes, SQL lexer/parser, rule-based query planner,
iterator-model executor, transactions with strict two-phase locking, a write-ahead
log, and ARIES-style crash recovery.

It is an **educational engine**. It is not production-ready and doesn't replace SQLite
or PostgreSQL. The [Known limitations](#known-limitations) section lists exactly where
it falls short. It has about 6,700 lines of engine code and 1,900 lines of tests.

```text
pydb> CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER);
Table created.

pydb> INSERT INTO users VALUES (1, 'Girish', 21), (2, 'Asha', 34), (3, 'Ravi', 17);
3 rows inserted.

pydb> CREATE INDEX idx_users_age ON users(age);
Index created.

pydb> EXPLAIN SELECT name FROM users WHERE age >= 18;
Query Plan
----------
Projection(name)
-> IndexScan(users, idx_users_age: age >= 18)

pydb> SELECT name, age FROM users WHERE age >= 18 ORDER BY age DESC;
+--------+-----+
| name   | age |
+--------+-----+
| Asha   | 34  |
| Girish | 21  |
+--------+-----+
2 rows returned.
```

---

## Contents

- [Why this project matters](#why-this-project-matters)
- [Features](#features)
- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Usage](#usage)
- [Testing & validation](#testing--validation)
- [Benchmarks](#benchmarks)
- [Known limitations](#known-limitations)
- [Future improvements](#future-improvements)
- [Project structure](#project-structure)
- [Documentation](#documentation)

---

## Why this project matters

Using a database teaches SQL. Building one shows what has to happen underneath for
SQL to be fast, correct and safe. PyDB was built to understand those mechanisms well
enough to implement them, test them and explain them:

- **Storage systems:** how rows become bytes in fixed-size pages, why a slot
  directory keeps record IDs stable, how free space is tracked and reused, and how
  checksums detect corruption.
- **Caching:** why a buffer pool is needed, how LRU eviction works with pinned pages,
  and why a dirty page cannot be written before its log record (the WAL rule).
- **Indexing:** how a B+ tree splits and grows, how linked leaves make range scans
  cheap, and how duplicate keys are handled.
- **Query processing:** tokenizing, recursive-descent parsing, building an AST,
  compiling expressions, three-valued NULL logic, and the iterator (Volcano) model.
- **Query optimization:** predicate pushdown, choosing an index scan over a
  sequential scan, and picking a join algorithm. All of it is visible through `EXPLAIN`.
- **Transactions & concurrency:** commit, rollback, savepoints, shared and exclusive
  locks, strict 2PL, lock upgrades, writer starvation and deadlock detection.
- **Durability & recovery:** write-ahead logging, LSNs, fsync at commit, steal/no-force
  buffering, compensation log records, and the analysis/redo/undo recovery passes.
- **Systems programming:** binary formats with `struct`, file I/O and `fsync`,
  cross-platform OS file locking, multi-threading, and testing against real process
  crashes.

Several real bugs only showed up in testing: a writer starving behind a stream of
readers, and an index meta page being reused after a crash. Finding and fixing those
was part of the learning.

---

## Features

All of the following is **implemented and tested**. Anything not implemented is under
[Known limitations](#known-limitations) or [Future improvements](#future-improvements).

| Area | Implemented |
|---|---|
| Storage | Fixed 4 KiB pages, CRC32 page checksums, slotted heap pages, variable-length records, page compaction, free-page reuse |
| Buffer pool | LRU replacement, pin counts, dirty tracking, write-back, hit/miss/eviction statistics, WAL rule |
| Catalog | `system_tables`, `system_columns`, `system_indexes` stored as ordinary heap tables, and queryable with SQL |
| SQL | `CREATE/DROP TABLE`, `CREATE [UNIQUE] INDEX`, `DROP INDEX`, multi-row `INSERT`, `SELECT`, `UPDATE`, `DELETE`, `BEGIN/COMMIT/ROLLBACK`, `EXPLAIN` |
| Queries | `WHERE`, `INNER/LEFT/CROSS JOIN`, `GROUP BY`, `HAVING`, `ORDER BY`, `LIMIT/OFFSET`, `DISTINCT`, aliases |
| Expressions | Comparisons, `AND/OR/NOT`, `IS [NOT] NULL`, `IN`, `BETWEEN`, `LIKE`, arithmetic, `UPPER LOWER LENGTH ABS ROUND COALESCE`, three-valued logic |
| Aggregates | `COUNT(*)`, `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `COUNT(DISTINCT …)` |
| Types | `INTEGER`, `FLOAT`, `TEXT`, `BOOLEAN`, `NULL` (plus aliases such as `INT`, `REAL`, `VARCHAR(n)`, `BOOL`) |
| Constraints | `PRIMARY KEY`, `UNIQUE`, `NOT NULL`, `DEFAULT`, `REFERENCES` (foreign keys, restrict semantics), all enforced |
| Indexes | Disk-based B+ tree: splits, linked leaves, range scans, duplicate keys, deletion |
| Planner | Predicate pushdown, index vs. sequential scan, range-bound merging, join algorithm selection |
| Executor | `SeqScan`, `IndexScan`, `Filter`, `Projection`, `Sort`, `Limit`, `Distinct`, `HashAggregate`, `NestedLoopJoin`, `HashJoin`, `IndexNestedLoopJoin` |
| Transactions | Autocommit and explicit transactions, statement-level atomicity, rollback with compensation log records |
| Concurrency | Table-level strict 2PL, shared/exclusive locks, upgrades, writer fairness, deadlock detection, lock timeouts |
| Durability | Write-ahead log with LSNs and CRC-protected records, fsync on commit, checkpoints |
| Recovery | Analysis / redo / undo from the WAL, index rebuild, orphan-page reclamation |
| Tools | Interactive SQL shell, Python API, page/WAL/B+ tree inspection scripts, benchmarks |
| Safety | `?` parameter binding (no SQL injection), no `eval`/`exec`, OS-level file lock |

---

## Architecture

```text
              SQL text  /  Python API (Database.execute)
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Lexer  →  Parser  →  AST             │  pydb/sql/
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Planner (rule-based optimizer)       │  pydb/execution/planner.py
          └───────────────────────────────────────┘
                              │  operator tree
                              ▼
          ┌───────────────────────────────────────┐
          │  Execution engine (iterator model)    │  pydb/execution/
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Transactions / Concurrency           │  pydb/transaction/
          │  strict 2PL locks, deadlock detection │
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Storage engine: heap files,          │  pydb/table.py, pydb/storage/
          │  constraints, index maintenance       │  pydb/constraints/, pydb/index/
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Buffer pool (LRU, WAL rule)          │  pydb/storage/buffer_pool.py
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Pages / Records / B+ tree nodes      │  page.py, record.py, bplus_tree.py
          └───────────────────────────────────────┘
                              │
                              ▼
          ┌───────────────────────────────────────┐
          │  Disk  (name.db)  +  WAL (name.db.wal)│  disk.py, wal.py
          └───────────────────────────────────────┘
                              │  on startup after a crash
                              ▼
          ┌───────────────────────────────────────┐
          │  Crash recovery: analysis/redo/undo   │  pydb/transaction/recovery.py
          └───────────────────────────────────────┘
```

Every change is logged *before* its page is modified, and the buffer pool flushes the
log before writing any dirty page. The catalog lives in ordinary heap tables, so schema
changes go through the same WAL and recovery path as user data.
[docs/architecture.md](docs/architecture.md) walks through the life of a query and of a
write, and lists every design decision with the alternative that was rejected.

---

## How it works

### Storage engine: 4 KiB pages and slotted records

- The database file is an array of **4096-byte pages**. That size matches the OS page and
  SSD sector size, and it is SQLite's default. Every page starts with a 24-byte header:
  page LSN, CRC32 checksum, page type, owner object and next-page pointer.
- Tables are **heap files**: linked lists of **slotted pages**. A slot directory grows from
  the front and records grow from the back. A row is addressed by
  `RID = (page_id, slot)`, and slots never move, so RIDs stay valid when a page is compacted.
- **Records** use a NULL bitmap followed by the non-NULL values. INTEGER and FLOAT take
  8 bytes, BOOLEAN 1 byte, and TEXT a 2-byte length plus UTF-8 bytes.
- A **free-space manager** tracks free pages and the free bytes in each heap page.

Details: [docs/storage.md](docs/storage.md).

### Buffer pool

256 frames (1 MiB) by default, with **LRU eviction** of unpinned pages and write-back of
dirty pages. The **WAL rule** applies: the log is flushed up to a page's LSN before that
page is written. Statistics (hits, misses, hit ratio, evictions, flushes) are shown by
`.stats`.

### Catalog

Table, column, index, constraint and default metadata is stored in three system heap
tables. You can query them, for example `SELECT * FROM system_indexes;`.

### SQL front end

A hand-written **lexer** and a **recursive-descent parser** produce an **AST**. The parser
uses one method per precedence level. Expressions are compiled once into Python
closures, with column positions resolved ahead of time. There is no `eval`, and `?`
parameters are bound as values, never spliced into SQL text.

### Query execution and planning

The executor uses the **iterator model**: every operator pulls rows from its children,
so `LIMIT` stops a scan early. The **rule-based planner** does the following:

- splits `WHERE` into conjuncts and pushes single-table predicates down;
- chooses an **index scan** for sargable predicates (`=`, `<`, `>=`, `BETWEEN`, …) on
  indexed columns, preferring equality and unique indexes and merging range bounds;
- otherwise uses a sequential scan;
- for equi-joins, picks an **index nested-loop join** when the inner column is indexed and
  a **hash join** otherwise; any other join condition uses a nested-loop join.

Details: [docs/query-engine.md](docs/query-engine.md).

### B+ tree indexes

- One node per page, with a fixed meta page that stores the root.
- Leaves are linked to each other for range scans.
- Duplicate keys are ordered as `(key, page_id, slot)`.
- Nodes split by page capacity: leaves copy their first key up, internal nodes move
  their middle key up, and a root split adds a level.
- PRIMARY KEY and UNIQUE columns get their indexes automatically.
- Deletes remove leaf entries without merging nodes.

Details: [docs/indexing.md](docs/indexing.md). In the shell, `.btree pk_users` prints a
tree.

### Transactions and concurrency

- `BEGIN … COMMIT / ROLLBACK`. Outside an explicit transaction, every statement
  autocommits.
- A failing statement is undone on its own, using a savepoint.
- Rollback writes **compensation log records** and undoes index changes.
- Concurrency uses **table-level strict two-phase locking**: shared and exclusive locks
  with upgrades.
- **Writer fairness**: new readers queue behind a waiting writer.
- **Deadlock detection** walks the wait-for graph, with timeouts as a backstop.
- Isolation is **serializable at table granularity, within one process**.

Details: [docs/transactions.md](docs/transactions.md).

### Write-ahead log and crash recovery

- The WAL is an append-only file of CRC-protected records with LSNs and full
  before/after images.
- `COMMIT` fsyncs the log, and that is the moment a transaction becomes durable.
- A checkpoint flushes all pages and truncates the log, so a clean shutdown leaves an
  empty log.
- On startup, a non-empty log triggers **recovery**:
  1. **analysis**: sort transactions into committed, aborted and in-flight;
  2. **redo**: repeat history for every record newer than its page;
  3. **undo**: roll back the in-flight transactions;
  4. reload the catalog, reclaim orphan pages, and rebuild the indexes of the tables
     that changed.

Details: [docs/recovery.md](docs/recovery.md).

---

## Installation

Requires **Python 3.10 or newer**. PyDB has no runtime dependencies. `requirements.txt`
only installs `pytest` and `ruff` for development.

Windows (PowerShell):

```powershell
git clone https://github.com/GIRISHKUSANURAMATH/PyDB-Database-From-Scratch.git
cd PyDB-Database-From-Scratch
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux / macOS:

```bash
git clone https://github.com/GIRISHKUSANURAMATH/PyDB-Database-From-Scratch.git
cd PyDB-Database-From-Scratch
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional: `pip install -e .` installs a `pydb` command.

---

## Usage

### Interactive shell

```bash
python -m pydb                      # opens data/pydb.db
python -m pydb data/school.db       # another database file
python -m pydb data/school.db -c "SELECT * FROM students;"
python -m pydb --debug              # verbose logging and full tracebacks
```

Statements end with `;` and can span several lines. Shell commands:

| Command | Purpose |
|---|---|
| `.tables`, `.schema [table]` | list tables / show `CREATE TABLE` |
| `.indexes [table]` | indexes with B+ tree height, leaves and entries |
| `.explain SQL` | query plan |
| `.btree INDEX` | print the B+ tree structure |
| `.stats` | buffer pool, WAL, transaction and lock statistics |
| `.wal` | records currently in the write-ahead log |
| `.checkpoint`, `.timer on/off`, `.read FILE` | checkpoint, timing, run a SQL file |
| `.help`, `.quit` / `.exit` | help, leave |

### Python API

```python
from pydb import Database

db = Database("school.db")
db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, marks INTEGER)")
db.execute("INSERT INTO students VALUES (?, ?, ?)", (1, "Girish", 90))

result = db.execute("SELECT * FROM students WHERE marks >= ?", (80,))
for row in result:
    print(row)                      # (1, 'Girish', 90)

with db.transaction():              # commits on success, rolls back on exception
    db.execute("UPDATE students SET marks = marks + 5 WHERE id = ?", (1,))

print(db.explain("SELECT * FROM students WHERE id = 1"))
db.close()
```

Convenience methods include `create_table`, `insert`, `select`, `update`, `delete`,
`tables`, `schema`, `indexes`, `stats`, `executemany` and `executescript`. Use
`session()` to get one connection per thread. Errors derive from `pydb.DatabaseError`.

### Examples and inspection tools

```bash
python examples/quickstart.py            # both API styles, persistence across reopen
python examples/social_network.py        # constraints, indexes, joins, aggregation, COMMIT/ROLLBACK, EXPLAIN
python examples/crash_recovery_demo.py   # a child process crashes mid-transaction; recovery fixes it

python scripts/inspect_db.py data/social_network.db       # summary of pages, tables, indexes, WAL
python scripts/dump_page.py data/social_network.db 4      # decode one raw page
python scripts/dump_wal.py data/social_network.db         # list WAL records
python scripts/dump_btree.py data/social_network.db pk_users
```

---

## Testing & validation

```bash
pytest          # 156 tests
ruff check .    # lint
```

The test suite covers:

- **Storage:** slotted pages, records, checksums and corruption detection, the buffer pool.
- **SQL:** every statement and clause, three-valued logic, joins, aggregates, and SQL
  injection through parameters.
- **B+ tree:** splits, height growth, ranges, duplicates, deletion and persistence.
- **Transactions:** commit, rollback, statement atomicity, constraints, isolation and
  deadlocks.
- **Crash recovery:** redo, undo, stolen pages, CLR correctness, a torn or corrupt WAL,
  a crash during recovery, a real process kill, and randomized crash schedules.
- **Model checks:** randomized tests compare PyDB with plain Python data structures.
  SQLite is not used anywhere.

**Verified results:**

| Environment | Result |
|---|---|
| Windows 11, Python 3.10.11 | **156 passed**, Ruff clean |
| Linux (development), Python 3.13 | 156 passed, Ruff clean |
| Linux, Python 3.10 | 156 passed |

On Windows, `scripts\windows_verify.bat` runs the whole check: it creates `.venv`,
installs requirements, then runs pytest, Ruff and `scripts/windows_verify.py`. That last
script runs end-to-end checks as real processes, and all of these passed on Windows 11:

- an interactive `python -m pydb` session (DDL, DML, index, join, GROUP BY, rollback,
  commit, EXPLAIN, constraint error);
- **persistence across processes**: 1,000 rows written by one process and read by another;
- a **3,000-key B+ tree** forced to height 7, whose invariants still hold after a reopen;
- **transaction rollback**, a reader blocked by an uncommitted writer, and **deadlock
  detection**;
- **crash recovery after a hard process termination** (`os._exit`): committed rows are
  kept and the in-flight transaction is fully undone;
- all three examples and all four inspection scripts.

---

## Benchmarks

```bash
python benchmarks/run_benchmarks.py --rows 5000
```

Measured on **Windows 11, Python 3.10.11**, with 5,000 rows (full table in
[benchmarks/RESULTS_windows.md](benchmarks/RESULTS_windows.md)):

| Benchmark | Result |
|---|---:|
| **Point lookup via B+ tree index** | **0.156 ms/query** |
| Point lookup via sequential scan | 21.578 ms/query |
| **Index speed-up** | **≈139×** |
| Range scan, ~1% of rows: index vs. sequential | 0.8 ms vs. 27.8 ms |
| Bulk insert (one transaction) | 3,717 rows/s |
| Autocommit insert (fsync per commit) | 1,551 rows/s |
| Full sequential scan | 228,835 rows/s |
| Startup (open a 0.5 MiB database) | 57 ms |

A 20,000-row run on the Linux development machine is in
[benchmarks/RESULTS.md](benchmarks/RESULTS.md). Benchmark numbers depend heavily on the
machine, the disk's fsync latency and the Python version, so compare them relative to
each other rather than as absolute figures.

---

## Known limitations

These come from the current codebase. They are deliberate simplifications, not bugs:

- **Concurrency:** locks are **table-level**, so writers to the same table run one after
  another. There is no MVCC and no row-level locking.
- **One process per database:** an OS file lock blocks a second process, which gets
  `DatabaseLockedError`. Threads use separate sessions, and one statement executes at a
  time.
- **Index recovery:** B+ tree page changes are not WAL-logged. After a crash, the indexes
  of tables changed since the last checkpoint are **rebuilt from the recovered table
  data**, so recovery time grows with those tables' size. Normal `ROLLBACK` still undoes
  index changes directly.
- **No full-page WAL images:** a page torn by a power cut is *detected* by its checksum but
  cannot be repaired.
- **Checkpoints** are sharp and need a moment with no active writers. A transaction that
  never finishes lets the log grow.
- **Rows must fit within one page** (at most 4,062 bytes; TEXT index keys at most 512
  bytes). There are no overflow pages.
- **No B+ tree node merge** after deletion. Leaves are not rebalanced, so a shrinking tree
  keeps its height.
- **Startup** scans every page header to rebuild free-space information, so it is O(file
  size). The database file never shrinks.
- **SQL not supported:** subqueries, views, `ALTER TABLE`, `RIGHT/FULL JOIN`, `UNION`,
  multi-column indexes, composite primary keys, date/time types, `ON DELETE CASCADE`, and
  `CREATE/DROP` inside an explicit transaction.
- **UNIQUE is checked row by row**, so `UPDATE t SET id = id + 1` can fail on a temporary
  duplicate.
- **Planner:** rule-based with row counts only (no histograms). Joins run in the order
  written, and sorts, hash tables and aggregates are fully in memory.

---

## Future improvements

None of these are implemented yet:

1. Row-level locking or MVCC, so writers to different rows don't wait for each other.
2. Full-page images in the WAL, so torn pages can be repaired.
3. WAL-logged B+ tree operations, so indexes don't need rebuilding after a crash.
4. A cost-based optimizer with column statistics and join reordering.
5. Overflow pages for large values, and a persisted free-space map for faster startup.
6. B+ tree node merging, multi-column indexes, fuzzy checkpoints, group commit and
   external sort.

---

## Project structure

```text
pydb/
  storage/      disk.py  page.py  record.py  heap_file.py  free_space.py  buffer_pool.py
  catalog/      catalog.py  schema.py
  index/        bplus_tree.py  index_manager.py
  sql/          lexer.py  parser.py  ast.py  expressions.py
  execution/    planner.py  executor.py  operators.py  scan.py  filter.py  projection.py
                join.py  aggregate.py  sort.py
  transaction/  transaction.py  lock_manager.py  wal.py  recovery.py
  constraints/  constraints.py
  table.py  engine.py  database.py  cli.py  inspector.py  errors.py  config.py
tests/          unit/  sql/  indexes/  transactions/  recovery/  integration/
benchmarks/     run_benchmarks.py, RESULTS.md, RESULTS_windows.md
examples/       quickstart.py, social_network.py, crash_recovery_demo.py
scripts/        inspection tools, windows_verify.bat / .py
docs/           architecture, storage, indexing, query engine, transactions, recovery
```

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | layers, life of a query and of a write, design decisions and trade-offs |
| [docs/storage.md](docs/storage.md) | file format, page header, slotted pages, records, free space, buffer pool |
| [docs/indexing.md](docs/indexing.md) | B+ tree layout, search, insert/split, delete, planner use |
| [docs/query-engine.md](docs/query-engine.md) | lexer, grammar, expressions, operators, planner rules |
| [docs/transactions.md](docs/transactions.md) | transaction model, locking, deadlocks, precise isolation level |
| [docs/recovery.md](docs/recovery.md) | WAL format, checkpoints, the recovery algorithm, how crashes are tested |

## License

[MIT](LICENSE) © 2026 Girish Kusanuramath
