"""Unit tests for pages, records, the disk manager and the buffer pool."""

from __future__ import annotations

import random

import pytest

from pydb.config import PAGE_SIZE
from pydb.errors import (
    BufferPoolFullError,
    DatabaseLockedError,
    PageCorruptionError,
    PageError,
    PageFullError,
    RecordTooLargeError,
    StorageError,
    TypeMismatchError,
)
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.disk import DiskManager
from pydb.storage.page import MAX_RECORD_SIZE, HeapPage, Page, PageType, verify_checksum
from pydb.storage.record import ColumnType, RecordSerializer, coerce_value


def new_heap_page() -> HeapPage:
    page = HeapPage(bytearray(PAGE_SIZE))
    page.init(owner_id=7)
    return page


# ----------------------------------------------------------------------------- slotted page
class TestHeapPage:
    def test_insert_and_get(self):
        page = new_heap_page()
        s0 = page.insert(b"hello")
        s1 = page.insert(b"world!")
        assert (s0, s1) == (0, 1)
        assert page.get(0) == b"hello"
        assert page.get(1) == b"world!"
        assert page.page_type is PageType.HEAP
        assert page.owner_id == 7

    def test_delete_frees_slot_for_reuse(self):
        page = new_heap_page()
        for i in range(3):
            page.insert(f"r{i}".encode())
        page.delete(1)
        assert not page.is_live(1)
        with pytest.raises(PageError):
            page.get(1)
        assert page.insert(b"new") == 1
        assert page.get(1) == b"new"

    def test_delete_is_idempotent(self):
        page = new_heap_page()
        page.insert(b"x")
        page.delete(0)
        page.delete(0)
        page.delete(42)
        assert list(page.records()) == []

    def test_fill_page_until_full(self):
        page = new_heap_page()
        rec = b"a" * 100
        count = 0
        while page.free_space_for_insert() >= len(rec):
            page.insert(rec)
            count += 1
        assert count == (PAGE_SIZE - 30) // (100 + 4)
        with pytest.raises(PageFullError):
            page.insert(rec)

    def test_compaction_reclaims_fragmented_space(self):
        page = new_heap_page()
        for _ in range(30):
            page.insert(b"b" * 100)
        for slot in range(0, 30, 2):
            page.delete(slot)
        # 15 holes of 100 bytes, but not contiguous: a 1000-byte record needs compaction
        free_before = page.total_free()
        slot = page.insert(b"c" * 1000)
        assert page.get(slot) == b"c" * 1000
        assert page.total_free() == free_before - 1000
        for s in range(1, 30, 2):  # surviving records untouched, same slots
            assert page.get(s) == b"b" * 100

    def test_put_update_grow_and_shrink(self):
        page = new_heap_page()
        page.insert(b"short")
        page.put(0, b"a much longer record value")
        assert page.get(0) == b"a much longer record value"
        page.put(0, b"tiny")
        assert page.get(0) == b"tiny"

    def test_put_specific_slot_beyond_directory(self):
        page = new_heap_page()
        page.put(5, b"five")
        assert page.slot_count == 6
        assert page.get(5) == b"five"
        assert page.find_free_slot() == 0

    def test_max_record_fits_exactly(self):
        page = new_heap_page()
        page.insert(b"z" * MAX_RECORD_SIZE)
        assert page.free_space_for_insert() < 1

    def test_random_operations_match_model(self):
        rng = random.Random(1)
        page = new_heap_page()
        model: dict[int, bytes] = {}
        for _ in range(2000):
            op = rng.random()
            if op < 0.5:
                rec = bytes(rng.randrange(256) for _ in range(rng.randint(1, 120)))
                if page.free_space_for_insert() >= len(rec):
                    model[page.insert(rec)] = rec
            elif op < 0.8 and model:
                slot = rng.choice(list(model))
                page.delete(slot)
                del model[slot]
            elif model:
                slot = rng.choice(list(model))
                rec = bytes(rng.randrange(256) for _ in range(rng.randint(1, 150)))
                try:
                    page.put(slot, rec)
                    model[slot] = rec
                except PageFullError:
                    pass
            assert dict(page.records()) == model


# ----------------------------------------------------------------------------- records
class TestRecords:
    def test_roundtrip_all_types(self):
        ser = RecordSerializer([ColumnType.INTEGER, ColumnType.TEXT, ColumnType.FLOAT, ColumnType.BOOLEAN])
        row = (-42, "Girish ಕನ್ನಡ", 3.5, True)
        assert ser.decode(ser.encode(row)) == row

    def test_nulls_take_no_space(self):
        ser = RecordSerializer([ColumnType.INTEGER, ColumnType.TEXT])
        assert ser.decode(ser.encode((None, None))) == (None, None)
        assert len(ser.encode((None, None))) == 1  # just the bitmap
        assert len(ser.encode((1, None))) == 1 + 8

    def test_many_columns_bitmap(self):
        types = [ColumnType.INTEGER] * 17
        ser = RecordSerializer(types)
        row = tuple(None if i % 3 == 0 else i for i in range(17))
        assert ser.decode(ser.encode(row)) == row

    def test_too_large(self):
        ser = RecordSerializer([ColumnType.TEXT])
        with pytest.raises(RecordTooLargeError):
            ser.encode(("x" * 5000,))

    def test_coercion(self):
        assert coerce_value(3, ColumnType.FLOAT) == 3.0
        assert coerce_value(1, ColumnType.BOOLEAN) is True
        assert coerce_value(4.0, ColumnType.INTEGER) == 4
        with pytest.raises(TypeMismatchError):
            coerce_value("12", ColumnType.INTEGER)
        with pytest.raises(TypeMismatchError):
            coerce_value(True, ColumnType.INTEGER)
        with pytest.raises(TypeMismatchError):
            coerce_value(2**70, ColumnType.INTEGER)

    def test_type_aliases(self):
        assert ColumnType.parse("int") is ColumnType.INTEGER
        assert ColumnType.parse("VARCHAR") is ColumnType.TEXT
        with pytest.raises(TypeMismatchError):
            ColumnType.parse("BLOB")


# ----------------------------------------------------------------------------- disk
class TestDisk:
    def test_pages_persist_across_reopen(self, tmp_path):
        path = str(tmp_path / "d.db")
        disk = DiskManager(path)
        pid = disk.allocate_page_id()
        page = HeapPage(bytearray(PAGE_SIZE))
        page.init(owner_id=3)
        page.insert(b"persist me")
        disk.write_page(pid, page.data)
        disk.close()

        disk = DiskManager(path)
        assert disk.num_pages == 2
        assert HeapPage(disk.read_page(pid)).get(0) == b"persist me"
        disk.close()

    def test_read_beyond_eof_is_free_page(self, tmp_path):
        disk = DiskManager(str(tmp_path / "d.db"))
        data = disk.read_page(99)
        assert Page(data).page_type is PageType.FREE
        disk.close()

    def test_corruption_detected(self, tmp_path):
        path = str(tmp_path / "d.db")
        disk = DiskManager(path)
        pid = disk.allocate_page_id()
        page = HeapPage(bytearray(PAGE_SIZE))
        page.init(owner_id=3)
        page.insert(b"important")
        disk.write_page(pid, page.data)
        disk.close()
        with open(path, "r+b") as fh:  # flip one byte inside the record area
            fh.seek(pid * PAGE_SIZE + PAGE_SIZE - 3)
            b = fh.read(1)
            fh.seek(pid * PAGE_SIZE + PAGE_SIZE - 3)
            fh.write(bytes([b[0] ^ 0xFF]))
        disk = DiskManager(path)
        with pytest.raises(PageCorruptionError):
            disk.read_page(pid)
        disk.close()

    def test_not_a_database(self, tmp_path):
        path = tmp_path / "junk.db"
        path.write_bytes(b"\x00" * PAGE_SIZE)
        with pytest.raises(StorageError):
            DiskManager(str(path))

    def test_second_open_is_refused(self, tmp_path):
        path = str(tmp_path / "d.db")
        disk = DiskManager(path)
        with pytest.raises(DatabaseLockedError):
            DiskManager(path)
        disk.close()
        DiskManager(path).close()  # lock released on close

    def test_zero_page_checksum_valid(self):
        assert verify_checksum(bytes(PAGE_SIZE))


# ----------------------------------------------------------------------------- buffer pool
class TestBufferPool:
    @pytest.fixture
    def disk(self, tmp_path):
        d = DiskManager(str(tmp_path / "bp.db"))
        for i in range(1, 11):
            page = HeapPage(bytearray(PAGE_SIZE))
            page.init(owner_id=i)
            d.write_page(d.allocate_page_id(), page.data)
        yield d
        d.close()

    def test_hit_and_miss(self, disk):
        pool = BufferPool(disk, capacity=4)
        with pool.pin(1):
            pass
        with pool.pin(1):
            pass
        assert pool.stats.misses == 1
        assert pool.stats.hits == 1
        assert pool.stats.hit_ratio == 0.5

    def test_lru_eviction_order(self, disk):
        pool = BufferPool(disk, capacity=4)
        for pid in (1, 2, 3, 4):
            with pool.pin(pid):
                pass
        with pool.pin(1):  # 1 becomes most recently used; 2 is now LRU
            pass
        with pool.pin(5):
            pass
        assert 2 not in pool
        assert {1, 3, 4, 5} == set(pool.resident_page_ids())
        assert pool.stats.evictions == 1

    def test_pinned_pages_are_not_evicted(self, disk):
        pool = BufferPool(disk, capacity=4)
        with pool.pin(1), pool.pin(2), pool.pin(3), pool.pin(4):
            with pytest.raises(BufferPoolFullError):
                with pool.pin(5):
                    pass

    def test_dirty_page_written_on_eviction(self, disk):
        pool = BufferPool(disk, capacity=4)
        flushed_lsns = []
        pool.wal_flush_to = flushed_lsns.append
        with pool.pin(1) as frame:
            page = HeapPage(frame.data)
            page.insert(b"dirty data")
            page.lsn = 77
            frame.dirty = True
        for pid in (2, 3, 4, 5):
            with pool.pin(pid):
                pass
        assert pool.stats.dirty_flushes == 1
        assert flushed_lsns == [77]  # WAL forced before the data page was written
        assert HeapPage(disk.read_page(1)).get(0) == b"dirty data"

    def test_flush_all(self, disk):
        pool = BufferPool(disk, capacity=8)
        for pid in (1, 2, 3):
            with pool.pin(pid) as frame:
                HeapPage(frame.data).insert(b"x%d" % pid)
                frame.dirty = True
        assert len(pool.dirty_page_ids()) == 3
        pool.flush_all()
        assert pool.dirty_page_ids() == []
        assert HeapPage(disk.read_page(3)).get(0) == b"x3"
