"""B+ tree tests: search, range scans, splits, duplicates, deletion, persistence."""

from __future__ import annotations

import random

import pytest

from pydb.errors import IndexError as PyDBIndexError
from pydb.index.bplus_tree import BPlusTree
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.disk import DiskManager
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.record import ColumnType


@pytest.fixture
def env(tmp_path):
    disk = DiskManager(str(tmp_path / "bt.db"))
    pool = BufferPool(disk, capacity=64)
    fsm = FreeSpaceManager(disk)
    yield disk, pool, fsm
    disk.close(sync=False)


def make_tree(env, key_type=ColumnType.INTEGER, max_entries=4) -> BPlusTree:
    _disk, pool, fsm = env
    return BPlusTree.create(pool, fsm, owner_id=500, key_type=key_type, max_entries=max_entries)


def test_insert_and_point_search(env):
    tree = make_tree(env)
    for i in range(50):
        tree.insert(i, (i, 0))
    for i in range(50):
        assert tree.search(i) == [(i, 0)]
    assert tree.search(999) == []
    tree.check_invariants()


def test_splits_grow_height(env):
    tree = make_tree(env, max_entries=4)
    assert tree.height() == 1
    for i in range(5):
        tree.insert(i, (1, i))
    assert tree.height() == 2  # first leaf split created a new root
    for i in range(5, 200):
        tree.insert(i, (1, i))
    stats = tree.stats()
    assert stats["height"] >= 4
    assert stats["entries"] == 200
    assert stats["internal_nodes"] >= 1
    tree.check_invariants()


def test_random_order_inserts_sorted_iteration(env):
    tree = make_tree(env, max_entries=5)
    keys = list(range(1000))
    random.Random(7).shuffle(keys)
    for k in keys:
        tree.insert(k, (k // 10, k % 10))
    assert [e[0] for e in tree] == sorted(keys)
    tree.check_invariants()


def test_range_scan_bounds(env):
    tree = make_tree(env, max_entries=4)
    for k in range(0, 100, 2):  # even numbers
        tree.insert(k, (k, 0))
    got = [e[0] for e in tree.range_entries(10, 20)]
    assert got == [10, 12, 14, 16, 18, 20]
    got = [e[0] for e in tree.range_entries(10, 20, low_inclusive=False, high_inclusive=False)]
    assert got == [12, 14, 16, 18]
    got = [e[0] for e in tree.range_entries(11, 15)]
    assert got == [12, 14]
    assert [e[0] for e in tree.range_entries(95)] == [96, 98]
    assert [e[0] for e in tree.range_entries(None, 4)] == [0, 2, 4]


def test_duplicate_keys(env):
    tree = make_tree(env, max_entries=4)
    for i in range(30):
        tree.insert(i % 3, (i, 0))  # keys 0,1,2 ten times each
    assert sorted(tree.search(1)) == [(i, 0) for i in range(1, 30, 3)]
    assert len(tree.search(0)) == 10
    tree.check_invariants()


def test_delete(env):
    tree = make_tree(env, max_entries=4)
    for i in range(100):
        tree.insert(i, (i, 0))
    for i in range(0, 100, 3):
        assert tree.delete(i, (i, 0))
    assert not tree.delete(0, (0, 0))  # already gone
    remaining = [e[0] for e in tree]
    assert remaining == [i for i in range(100) if i % 3]
    tree.check_invariants()
    for i in range(100):  # delete everything; tree stays valid and empty
        tree.delete(i, (i, 0))
    assert list(tree) == []
    tree.insert(5, (5, 5))
    assert tree.search(5) == [(5, 5)]


def test_text_keys(env):
    tree = make_tree(env, key_type=ColumnType.TEXT, max_entries=None)
    words = [f"user_{i:05d}_" + "x" * (i % 40) for i in range(3000)]
    random.Random(3).shuffle(words)
    for i, w in enumerate(words):
        tree.insert(w, (i, 0))
    assert [e[0] for e in tree] == sorted(words)
    assert tree.height() >= 2  # page-capacity splits without an artificial limit
    tree.check_invariants()
    with pytest.raises(PyDBIndexError):
        tree.insert("y" * 600, (0, 0))


def test_float_and_bool_keys(env):
    ftree = make_tree(env, key_type=ColumnType.FLOAT)
    for v in [3.5, -1.25, 0.0, 2.0, 10.75]:
        ftree.insert(v, (0, 0))
    assert [e[0] for e in ftree] == [-1.25, 0.0, 2.0, 3.5, 10.75]
    assert ftree.search(2) == [(0, 0)]  # int probe matches float key
    btree = make_tree(env, key_type=ColumnType.BOOLEAN)
    btree.insert(True, (1, 0))
    btree.insert(False, (2, 0))
    assert btree.search(True) == [(1, 0)]


def test_null_keys_rejected(env):
    tree = make_tree(env)
    with pytest.raises(PyDBIndexError):
        tree.insert(None, (0, 0))


def test_persistence_after_reopen(tmp_path):
    path = str(tmp_path / "persist.db")
    disk = DiskManager(path)
    pool = BufferPool(disk, capacity=16)  # small pool forces evictions mid-build
    fsm = FreeSpaceManager(disk)
    tree = BPlusTree.create(pool, fsm, owner_id=9, key_type=ColumnType.INTEGER, max_entries=8)
    meta = tree.meta_page_id
    for i in range(2000):
        tree.insert(i * 7 % 2003, (i, 1))
    pool.flush_all()
    disk.close()

    disk = DiskManager(path)
    pool = BufferPool(disk, capacity=16)
    tree = BPlusTree(pool, FreeSpaceManager(disk), meta, owner_id=9, max_entries=8)
    assert len(list(tree)) == 2000
    assert tree.search(7) == [(1, 1)]
    tree.check_invariants()
    disk.close()


def test_randomized_against_sorted_list(env):
    rng = random.Random(42)
    tree = make_tree(env, max_entries=6)
    model: set[tuple[int, int, int]] = set()
    for step in range(4000):
        if rng.random() < 0.65 or not model:
            k = rng.randint(0, 300)
            rid = (rng.randint(0, 50), rng.randint(0, 20))
            tree.insert(k, rid)
            model.add((k, *rid))
        else:
            entry = rng.choice(sorted(model))
            assert tree.delete(entry[0], (entry[1], entry[2]))
            model.discard(entry)
        if step % 500 == 0:
            lo, hi = sorted(rng.sample(range(301), 2))
            expected = [e for e in sorted(model) if lo <= e[0] <= hi]
            assert list(tree.range_entries(lo, hi)) == expected
    assert list(tree) == sorted(model)
    tree.check_invariants()
