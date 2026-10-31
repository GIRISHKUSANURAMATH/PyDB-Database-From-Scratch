"""Page formats.

Every page in the database file is exactly ``PAGE_SIZE`` bytes and starts with
the same 24-byte **common header**::

    offset size field
    0      8    page_lsn   LSN of the last WAL record applied to this page
    8      4    checksum   CRC32 of the page (computed with this field zeroed)
    12     1    page_type  FREE / HEADER / HEAP / BTREE_META / BTREE_LEAF / BTREE_INTERNAL
    13     1    flags      reserved
    14     4    owner_id   catalog object id (table or index) that owns the page
    18     4    next_page  heap: next page in the table's chain; B+ tree leaf: right sibling
    22     2    reserved

The ``page_lsn`` is what makes WAL redo idempotent: a log record is re-applied
only if ``record.lsn > page.page_lsn``.

The ``owner_id`` makes every page self-describing, which lets the engine
rebuild its free-page list by scanning the file and lets inspection tools
explain what each page is.

:class:`HeapPage` adds a **slotted-page** layout used for table rows::

    +--------------------------------------------+ 0
    | common header (24 bytes)                   |
    | slot_count u16 | free_end u16 | pad u16    | 24..30
    +--------------------------------------------+ 30
    | slot directory: slot_count x (offset u16,  |
    |                               length u16)  |   grows  -->
    +--------------------------------------------+
    |               free space                   |
    +--------------------------------------------+ free_end
    |   record bytes (packed at the end)         |   <-- grows
    +--------------------------------------------+ PAGE_SIZE

A record is addressed by ``(page_id, slot)``. Slots never move, so a record id
(RID) stays valid even when the page is compacted — only the offset stored in
the slot changes. A slot with offset 0 is empty (deleted) and can be reused.
"""

from __future__ import annotations

import struct
import zlib
from enum import IntEnum

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import PageError, PageFullError

COMMON_HEADER = struct.Struct("<QIBBIIH")  # lsn, checksum, type, flags, owner, next, reserved
COMMON_HEADER_SIZE = COMMON_HEADER.size  # 24
assert COMMON_HEADER_SIZE == 24

_CHECKSUM_OFFSET = 8
_TYPE_OFFSET = 12
_OWNER_OFFSET = 14
_NEXT_OFFSET = 18

HEAP_HEADER = struct.Struct("<HHH")  # slot_count, free_end, pad
HEAP_HEADER_OFFSET = COMMON_HEADER_SIZE
SLOT_DIR_OFFSET = COMMON_HEADER_SIZE + HEAP_HEADER.size  # 30
SLOT = struct.Struct("<HH")  # offset, length
SLOT_SIZE = SLOT.size

#: Largest record a heap page can hold (one slot, empty page).
MAX_RECORD_SIZE = PAGE_SIZE - SLOT_DIR_OFFSET - SLOT_SIZE


class PageType(IntEnum):
    FREE = 0
    HEADER = 1
    HEAP = 2
    BTREE_META = 3
    BTREE_LEAF = 4
    BTREE_INTERNAL = 5


# ----------------------------------------------------------------------------- checksums
def compute_checksum(data: bytes | bytearray) -> int:
    """CRC32 over the page with the checksum field treated as zero."""
    crc = zlib.crc32(memoryview(data)[:_CHECKSUM_OFFSET])
    crc = zlib.crc32(b"\x00\x00\x00\x00", crc)
    return zlib.crc32(memoryview(data)[_CHECKSUM_OFFSET + 4 :], crc)


def stamp_checksum(data: bytearray) -> None:
    struct.pack_into("<I", data, _CHECKSUM_OFFSET, compute_checksum(data))


def verify_checksum(data: bytes | bytearray) -> bool:
    stored = struct.unpack_from("<I", data, _CHECKSUM_OFFSET)[0]
    if stored == 0 and not any(data):
        return True  # a never-written (all-zero) page is a valid FREE page
    return stored == compute_checksum(data)


# ----------------------------------------------------------------------------- base page
class Page:
    """A typed view over a ``bytearray`` of exactly ``PAGE_SIZE`` bytes.

    The view does not copy: it edits the buffer-pool frame in place.
    """

    __slots__ = ("data",)

    def __init__(self, data: bytearray) -> None:
        if len(data) != PAGE_SIZE:
            raise PageError(f"page buffer must be {PAGE_SIZE} bytes, got {len(data)}")
        self.data = data

    # header fields ------------------------------------------------------------
    @property
    def lsn(self) -> int:
        return struct.unpack_from("<Q", self.data, 0)[0]

    @lsn.setter
    def lsn(self, value: int) -> None:
        struct.pack_into("<Q", self.data, 0, value)

    @property
    def page_type(self) -> PageType:
        return PageType(self.data[_TYPE_OFFSET])

    @property
    def owner_id(self) -> int:
        return struct.unpack_from("<I", self.data, _OWNER_OFFSET)[0]

    @property
    def next_page(self) -> int:
        return struct.unpack_from("<I", self.data, _NEXT_OFFSET)[0]

    @next_page.setter
    def next_page(self, value: int) -> None:
        struct.pack_into("<I", self.data, _NEXT_OFFSET, value)

    def format(self, page_type: PageType, owner_id: int, lsn: int = 0) -> None:
        """Wipe the page and write a fresh common header."""
        self.data[:] = bytes(PAGE_SIZE)
        COMMON_HEADER.pack_into(self.data, 0, lsn, 0, int(page_type), 0, owner_id, INVALID_PAGE_ID, 0)


def read_common_header(data: bytes | bytearray) -> tuple[int, PageType, int, int]:
    """Return ``(lsn, page_type, owner_id, next_page)`` without building a Page."""
    lsn, _crc, ptype, _flags, owner, nxt, _ = COMMON_HEADER.unpack_from(data, 0)
    return lsn, PageType(ptype), owner, nxt


# ----------------------------------------------------------------------------- heap page
class HeapPage(Page):
    """Slotted page holding variable-length table records."""

    __slots__ = ()

    def init(self, owner_id: int, lsn: int = 0) -> None:
        self.format(PageType.HEAP, owner_id, lsn)
        HEAP_HEADER.pack_into(self.data, HEAP_HEADER_OFFSET, 0, PAGE_SIZE, 0)

    # header -------------------------------------------------------------------
    @property
    def slot_count(self) -> int:
        return struct.unpack_from("<H", self.data, HEAP_HEADER_OFFSET)[0]

    def _set_slot_count(self, n: int) -> None:
        struct.pack_into("<H", self.data, HEAP_HEADER_OFFSET, n)

    @property
    def free_end(self) -> int:
        value = struct.unpack_from("<H", self.data, HEAP_HEADER_OFFSET + 2)[0]
        return PAGE_SIZE if value == 0 else value  # 0 encodes 4096 (u16 overflow guard)

    def _set_free_end(self, value: int) -> None:
        struct.pack_into("<H", self.data, HEAP_HEADER_OFFSET + 2, 0 if value == PAGE_SIZE else value)

    def _slot(self, slot: int) -> tuple[int, int]:
        return SLOT.unpack_from(self.data, SLOT_DIR_OFFSET + slot * SLOT_SIZE)

    def _set_slot(self, slot: int, offset: int, length: int) -> None:
        SLOT.pack_into(self.data, SLOT_DIR_OFFSET + slot * SLOT_SIZE, offset, length)

    # space accounting ------------------------------------------------------------
    def contiguous_free(self) -> int:
        return self.free_end - (SLOT_DIR_OFFSET + self.slot_count * SLOT_SIZE)

    def used_record_bytes(self) -> int:
        n = self.slot_count
        raw = self.data[SLOT_DIR_OFFSET : SLOT_DIR_OFFSET + n * SLOT_SIZE]
        return sum(length for off, length in SLOT.iter_unpack(raw) if off)

    def total_free(self) -> int:
        """Free bytes available after compaction (ignores slot reuse)."""
        return PAGE_SIZE - SLOT_DIR_OFFSET - self.slot_count * SLOT_SIZE - self.used_record_bytes()

    def free_space_for_insert(self) -> int:
        """Largest record that :meth:`insert` could store right now (single pass)."""
        n = self.slot_count
        used, has_empty_slot = 0, False
        for off, length in SLOT.iter_unpack(self.data[SLOT_DIR_OFFSET : SLOT_DIR_OFFSET + n * SLOT_SIZE]):
            if off:
                used += length
            else:
                has_empty_slot = True
        free = PAGE_SIZE - SLOT_DIR_OFFSET - n * SLOT_SIZE - used
        return free - (0 if has_empty_slot else SLOT_SIZE)

    # record access --------------------------------------------------------------
    def is_live(self, slot: int) -> bool:
        return 0 <= slot < self.slot_count and self._slot(slot)[0] != 0

    def get(self, slot: int) -> bytes:
        if not self.is_live(slot):
            raise PageError(f"slot {slot} is empty")
        off, length = self._slot(slot)
        return bytes(self.data[off : off + length])

    def records(self):
        """Yield ``(slot, bytes)`` for every live record."""
        for i in range(self.slot_count):
            off, length = self._slot(i)
            if off:
                yield i, bytes(self.data[off : off + length])

    def find_free_slot(self) -> int:
        n = self.slot_count
        raw = self.data[SLOT_DIR_OFFSET : SLOT_DIR_OFFSET + n * SLOT_SIZE]
        for i, (off, _length) in enumerate(SLOT.iter_unpack(raw)):
            if off == 0:
                return i
        return n

    def insert(self, record: bytes) -> int:
        """Store a record in the first free slot and return the slot number."""
        slot = self.find_free_slot()
        self.put(slot, record)
        return slot

    def put(self, slot: int, record: bytes) -> None:
        """Make ``slot`` contain exactly ``record`` (insert, overwrite or restore).

        This single primitive is used for normal inserts/updates *and* for WAL
        redo/undo. Because it is "set slot to value", applying it twice has
        the same effect as applying it once — the property recovery relies on.
        """
        if not record:
            raise PageError("empty records are not supported")
        needed_slots = max(0, slot + 1 - self.slot_count)
        old_len = self._slot(slot)[1] if slot < self.slot_count and self._slot(slot)[0] else 0
        available = self.total_free() + old_len - needed_slots * SLOT_SIZE
        if len(record) > available:
            raise PageFullError(f"record of {len(record)} bytes does not fit (free {available})")

        if slot < self.slot_count:
            off, length = self._slot(slot)
            if off and len(record) <= length:  # overwrite in place
                self.data[off : off + len(record)] = record
                self._set_slot(slot, off, len(record))
                return
            if off:
                self._set_slot(slot, 0, 0)  # free old copy, then re-place below
        else:
            for i in range(self.slot_count, slot + 1):  # grow directory with empty slots
                self._set_slot(i, 0, 0)
            self._set_slot_count(slot + 1)

        if self.contiguous_free() < len(record):
            self.compact()
        new_end = self.free_end - len(record)
        self.data[new_end : self.free_end] = record
        self._set_free_end(new_end)
        self._set_slot(slot, new_end, len(record))

    def delete(self, slot: int) -> None:
        """Empty a slot. Deleting an empty slot is a no-op (idempotent for recovery)."""
        if slot < self.slot_count:
            self._set_slot(slot, 0, 0)
            self._trim_slot_directory()

    def _trim_slot_directory(self) -> None:
        n = self.slot_count
        while n and self._slot(n - 1)[0] == 0:
            n -= 1
        self._set_slot_count(n)

    def compact(self) -> None:
        """Defragment: slide every live record to the end of the page.

        Slot numbers are preserved, so RIDs held by indexes remain valid.
        """
        live = [(i, *self._slot(i)) for i in range(self.slot_count)]
        live = [(i, bytes(self.data[off : off + ln])) for i, off, ln in live if off]
        end = PAGE_SIZE
        for i, rec in live:
            end -= len(rec)
            self.data[end : end + len(rec)] = rec
            self._set_slot(i, end, len(rec))
        dir_end = SLOT_DIR_OFFSET + self.slot_count * SLOT_SIZE
        self.data[dir_end:end] = bytes(end - dir_end)
        self._set_free_end(end)
