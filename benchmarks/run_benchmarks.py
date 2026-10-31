"""PyDB benchmarks — measured, not estimated.

    python benchmarks/run_benchmarks.py            # 20,000 rows (default)
    python benchmarks/run_benchmarks.py --rows 50000

Writes a Markdown report to benchmarks/RESULTS.md (with machine details) and
prints it. Numbers depend heavily on the machine, the disk (fsync latency!)
and the Python version, so treat them as relative, not absolute.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydb import Database, __version__  # noqa: E402


class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.seconds = time.perf_counter() - self.start


def run(rows: int, lookups: int, out_path: str | None) -> str:
    workdir = tempfile.mkdtemp(prefix="pydb-bench-")
    path = os.path.join(workdir, "bench.db")
    rng = random.Random(1)
    results: list[tuple[str, str, str]] = []

    def record(name: str, value: str, note: str = "") -> None:
        results.append((name, value, note))
        print(f"  {name:<44} {value:>18}  {note}")

    print(f"PyDB {__version__} benchmark: {rows:,} rows, {lookups} lookups, temp dir {workdir}")
    try:
        db = Database(path)
        db.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, code INTEGER, category INTEGER, name TEXT, price FLOAT)"
        )

        # ---------------------------------------------------------------- inserts
        with Timer() as t:
            with db.transaction():
                for i in range(rows):
                    db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?)",
                               (i, rows - i, i % 100, f"item-{i:06d}", float(i % 997)))
        record("bulk insert (1 transaction)", f"{rows / t.seconds:,.0f} rows/s", f"{t.seconds:.2f} s total")

        n_auto = min(300, rows // 10)
        with Timer() as t:
            for i in range(rows, rows + n_auto):
                db.execute("INSERT INTO items VALUES (?, ?, ?, ?, ?)", (i, -i, 0, "auto", 1.0))
        record("autocommit insert (fsync per commit)", f"{n_auto / t.seconds:,.0f} rows/s",
               f"{t.seconds / n_auto * 1000:.2f} ms per commit")
        total_rows = rows + n_auto

        # ---------------------------------------------------------------- sequential scan
        with Timer() as t:
            n = len(db.execute("SELECT * FROM items").rows)
        assert n == total_rows
        record("full sequential scan", f"{n / t.seconds:,.0f} rows/s", f"{t.seconds * 1000:.1f} ms for {n:,} rows")

        # ---------------------------------------------------------------- point lookups: index vs seq scan
        keys = [rng.randrange(rows) for _ in range(lookups)]
        with Timer() as t_idx:
            for k in keys:
                assert db.execute("SELECT name FROM items WHERE id = ?", (k,)).rows
        # same selectivity (unique values), but 'code' has no index -> sequential scan
        seq_keys = keys[: max(5, lookups // 20)]
        with Timer() as t_seq:
            for k in seq_keys:
                assert db.execute("SELECT name FROM items WHERE code = ?", (rows - k,)).rows
        idx_ms = t_idx.seconds / len(keys) * 1000
        seq_ms = t_seq.seconds / len(seq_keys) * 1000
        assert "IndexScan" in db.explain("SELECT name FROM items WHERE id = 1")
        assert "SeqScan" in db.explain("SELECT name FROM items WHERE code = 1")
        record("point lookup via B+ tree (IndexScan)", f"{idx_ms:.3f} ms/query", f"{len(keys)} queries")
        record("point lookup without index (SeqScan)", f"{seq_ms:.3f} ms/query", f"{len(seq_keys)} queries")
        record("  -> index speed-up", f"{seq_ms / idx_ms:,.0f}x", "")

        # ---------------------------------------------------------------- range scans
        db.execute("CREATE INDEX idx_items_price ON items(price)")
        lo, hi = 100.0, 109.0  # ~1% of the rows
        with Timer() as t_ir:
            n_ir = len(db.execute("SELECT id FROM items WHERE price BETWEEN ? AND ?", (lo, hi)).rows)
        db.execute("DROP INDEX idx_items_price")
        with Timer() as t_sr:
            n_sr = len(db.execute("SELECT id FROM items WHERE price BETWEEN ? AND ?", (lo, hi)).rows)
        assert n_ir == n_sr
        record("range scan ~1% rows, B+ tree", f"{t_ir.seconds * 1000:.1f} ms", f"{n_ir} rows")
        record("range scan ~1% rows, sequential", f"{t_sr.seconds * 1000:.1f} ms", f"{n_sr} rows")

        # ---------------------------------------------------------------- index build
        with Timer() as t:
            db.execute("CREATE INDEX idx_items_category ON items(category)")
        record("CREATE INDEX on existing table", f"{t.seconds * 1000:.0f} ms", f"{total_rows:,} rows")
        tree_stats = db.indexes("items")
        pk = next(i for i in tree_stats if i["name"] == "pk_items")
        record("primary-key B+ tree shape", f"height {pk['height']}",
               f"{pk['leaf_nodes']} leaves, {pk['internal_nodes']} internal nodes")

        # ---------------------------------------------------------------- updates
        n_upd = min(2000, rows)
        upd_keys = [rng.randrange(rows) for _ in range(n_upd)]
        with Timer() as t:
            with db.transaction():
                for k in upd_keys:
                    db.execute("UPDATE items SET price = price + 1 WHERE id = ?", (k,))
        record("UPDATE by primary key (1 transaction)", f"{n_upd / t.seconds:,.0f} rows/s", f"{n_upd} updates")

        # ---------------------------------------------------------------- aggregation / join
        with Timer() as t:
            groups = db.execute("SELECT category, COUNT(*), AVG(price) FROM items GROUP BY category").rows
        record("GROUP BY over whole table", f"{t.seconds * 1000:.0f} ms", f"{len(groups)} groups")

        stats = db.stats()["buffer_pool"]
        record("buffer pool hit ratio (whole run)", f"{stats['hit_ratio']:.2%}",
               f"{stats['hits']:,} hits / {stats['misses']:,} misses, {stats['evictions']:,} evictions, "
               f"{stats['capacity']} frames")
        db.close()

        size = os.path.getsize(path)
        with Timer() as t:
            db = Database(path)
        record("startup time (open existing DB)", f"{t.seconds * 1000:.0f} ms",
               f"file {size / 1024 / 1024:.1f} MiB, {db.stats()['pages']:,} pages scanned")
        db.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    lines = [
        "# PyDB benchmark results",
        "",
        f"Generated by `python benchmarks/run_benchmarks.py --rows {rows}` — real measurements from one run.",
        "",
        f"- PyDB {__version__}, Python {platform.python_version()} ({platform.python_implementation()})",
        f"- {platform.system()} {platform.release()} on {platform.machine()}",
        f"- {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| Benchmark | Result | Notes |",
        "|---|---:|---|",
    ]
    lines += [f"| {name.strip()} | {value} | {note} |" for name, value, note in results]
    lines += [
        "",
        "Interpretation: the B+ tree turns a point lookup from reading every page of the",
        "table (O(n)) into a root-to-leaf walk of a few pages (O(log n)). Autocommit inserts",
        "pay for a WAL fsync on every COMMIT, so batching writes in one transaction is faster;",
        "how large the gap is depends on the fsync latency of the disk.",
    ]
    report = "\n".join(lines) + "\n"
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(report)
        print(f"\nReport written to {out_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--lookups", type=int, default=500)
    parser.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "RESULTS.md"))
    args = parser.parse_args()
    run(args.rows, args.lookups, args.out)


if __name__ == "__main__":
    main()
