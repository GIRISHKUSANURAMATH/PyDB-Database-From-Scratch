# Storage engine

Source: `pydb/storage/`.

## Files on disk

| File | Contents |
|---|---|
| `name.db` | Array of 4096-byte pages. Page *n* starts at byte `n * 4096`. |
| `name.db.wal` | Write-ahead log (see [recovery.md](recovery.md)). |
| `name.db.lock` | Empty file holding an OS lock (`msvcrt` on Windows, `fcntl` elsewhere) so only one process opens the database. The OS releases the lock if the process dies. |

## Common page header (every page, 24 bytes)

```text
offset size field
0      8    page_lsn    LSN of the last WAL record applied to the page
8      4    checksum    CRC32 of the whole page, computed with this field = 0
12     1    page_type   0 FREE, 1 HEADER, 2 HEAP, 3 BTREE_META, 4 BTREE_LEAF, 5 BTREE_INTERNAL
13     1    flags       reserved
14     4    owner_id    catalog id of the table/index that owns the page
18     4    next_page   heap: next page of the table; B+ tree leaf: right sibling
22     2    reserved
```

* **page_lsn** makes redo idempotent: a log record is re-applied only when
  `record.lsn > page_lsn`.
* **checksum** is verified on every read (`DiskManager.read_page`). A mismatch raises
  `PageCorruptionError` instead of returning garbage. An all-zero page (allocated but
  never written) is accepted as a FREE page.
* **owner_id** makes pages self-describing; the startup scan uses it to rebuild free-space
  information and to reclaim pages whose owner was dropped.

## Page 0 — file header

After the common header: magic `PYDB`, format version (u16), page size (u32) and the
*checkpoint LSN* (u64). The checkpoint LSN carries the LSN counter across WAL
truncations so LSNs never go backwards.

Pages 1, 2, 3 are the first pages of the three catalog tables.

## Heap page (slotted page)

```text
+--------------------------------------------+ 0
| common header (24 bytes)                   |
| slot_count u16 | free_end u16 | pad u16    | 24..30
+--------------------------------------------+ 30
| slot directory: slot_count x               |
|   (offset u16, length u16)        grows -> |
+--------------------------------------------+
|               free space                   |
+--------------------------------------------+ free_end
|  <- grows        record bytes              |
+--------------------------------------------+ 4096
```

* A record id (**RID**) is `(page_id, slot)`. Slots never move, so a RID stays valid when
  the page is defragmented — only the offset stored in the slot changes.
* A slot with offset 0 is empty. Inserts reuse the first empty slot; trailing empty
  slots are trimmed from the directory.
* `HeapPage.put(slot, bytes)` is the one primitive used for insert, update, redo and undo.
  If the free space is fragmented, `compact()` slides all live records to the end.
* Largest record: `4096 - 30 - 4 = 4062` bytes. There are no overflow pages.

Example (from `python scripts/dump_page.py data/pydb.db 4`):

```text
Page 4 of data/pydb.db
  type:       HEAP
  page LSN:   44
  checksum:   0xdf536772 (OK)
  owner:      100 (users)
  next page:  -
  slots:      4
  Slot directory:
    [  0] offset=4071  len=25   0001000000000000000600476972697368...  (1, 'Girish', 22)
    [  1] offset=4048  len=23   00020000000000000004004173686122...    (2, 'Asha', 34)
```

## Record layout

```text
+--------------------+-----------------------------------------------+
| null bitmap        | values of the non-NULL columns, in order       |
| ceil(ncols/8) bytes|                                                |
+--------------------+-----------------------------------------------+
INTEGER  8 bytes signed little-endian      FLOAT   8-byte IEEE double
BOOLEAN  1 byte                            TEXT    u16 length + UTF-8 bytes
```

NULLs cost one bit. `(1, 'Girish', 22)` is `00 | 01 00.. (8) | 06 00 'Girish' | 16 00..` = 25 bytes.

## Heap files

A table is a linked list of heap pages starting at `first_page` (stored in
`system_tables`). `HeapFile` (`heap_file.py`):

* `insert` → asks the free-space map for a page with room, otherwise allocates a new page
  and links it after the last one (logged as `NEW_PAGE`).
* `update` → in place if the new version fits in the same page (after compaction),
  otherwise delete + insert at a new RID (the caller updates the indexes).
* `scan` → follows the chain, copying each page's records out before unpinning it.

Every change is logged *before* it is applied to the page (see the module docstring).

## Free-space management

`FreeSpaceManager` keeps, in memory:

* a sorted **free page list** (pages typed FREE), reused before the file is extended;
* a **free-space map** per table: page → free bytes (only pages with ≥ 32 free bytes);
* the **last page** of each heap chain.

It is rebuilt at every startup by one pass over all page headers. A page whose
`owner_id` is not in the catalog (e.g. the table was dropped and the process crashed
before its pages were wiped) is wiped and returned to the free list. The startup cost is
O(file size) — about 11 ms for a 2 MiB file in the benchmark.

## Buffer pool

`BufferPool` (`buffer_pool.py`) caches up to `buffer_pool_size` pages (default 256 = 1 MiB).

* `with pool.pin(pid) as frame:` — fetch and pin; pinned frames are never evicted.
* Replacement policy: **LRU** using an `OrderedDict` ordered from least to most recently
  used; the first unpinned frame is the victim.
* Write-back caching: modified frames are marked dirty and written only on eviction,
  `flush_all()` or checkpoint.
* **WAL rule:** before writing a dirty page the pool calls `wal.flush_to(page_lsn)`.
* Statistics: hits, misses, hit ratio, evictions, dirty flushes — shown by `.stats`.

Because pages can be written while their transaction is still running (a *steal* policy)
and committed pages need not be written at commit (*no-force*), recovery needs both undo
and redo. That is exactly the ARIES combination.
