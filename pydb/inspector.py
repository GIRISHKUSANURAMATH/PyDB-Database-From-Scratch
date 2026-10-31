"""Read-only inspection of a database file, for learning and debugging.

These helpers read the raw file directly — they do not start the engine, run
recovery or take the file lock — so you can look at a database exactly as it
is on disk, even right after a crash (before recovery has run).
Used by ``scripts/inspect_db.py``, ``scripts/dump_page.py`` and ``scripts/dump_wal.py``.
"""

from __future__ import annotations

import os
import struct
from collections import Counter

from pydb.catalog.catalog import SYSTEM_SCHEMAS
from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.index.bplus_tree import _CODE_TYPES, _META, _decode_node
from pydb.storage.disk import FILE_HEADER
from pydb.storage.page import (
    COMMON_HEADER_SIZE,
    SLOT,
    SLOT_DIR_OFFSET,
    HeapPage,
    PageType,
    read_common_header,
    verify_checksum,
)
from pydb.transaction.wal import WAL_HEADER, WriteAheadLog


class RawFile:
    def __init__(self, path: str) -> None:
        self.path = path
        with open(path, "rb") as fh:
            self.data = fh.read()
        self.num_pages = len(self.data) // PAGE_SIZE

    def page(self, pid: int) -> bytearray:
        if not 0 <= pid < self.num_pages:
            raise SystemExit(f"page {pid} does not exist (file has {self.num_pages} pages)")
        return bytearray(self.data[pid * PAGE_SIZE : (pid + 1) * PAGE_SIZE])

    def heap_rows(self, first_page: int, serializer):
        pid = first_page
        while pid != INVALID_PAGE_ID and pid < self.num_pages:
            page = HeapPage(self.page(pid))
            if page.page_type is not PageType.HEAP:
                break
            for _slot, rec in page.records():
                yield serializer.decode(rec)
            pid = page.next_page


def _fmt_pid(pid: int) -> str:
    return "-" if pid == INVALID_PAGE_ID else str(pid)


def owner_names(raw: RawFile) -> dict[int, str]:
    names = {s.id: s.name for s in SYSTEM_SCHEMAS}
    tables, _cols, indexes = SYSTEM_SCHEMAS
    for tid, name, _first in raw.heap_rows(tables.first_page, tables.serializer):
        names[tid] = name
    for row in raw.heap_rows(indexes.first_page, indexes.serializer):
        names[row[0]] = f"index {row[1]}"
    return names


def describe_database(path: str) -> str:
    raw = RawFile(path)
    lines = [f"Database: {path}", ""]
    magic, version, page_size, ckpt = FILE_HEADER.unpack_from(raw.page(0), COMMON_HEADER_SIZE)
    wal_path = path + ".wal"
    wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
    lines += [
        f"Format:            {magic.decode()} v{version}, page size {page_size} bytes",
        f"File size:         {len(raw.data):,} bytes",
        f"Pages:             {raw.num_pages}",
        f"Checkpoint LSN:    {ckpt}",
        f"WAL size:          {wal_size:,} bytes"
        + ("  (clean shutdown)" if wal_size <= WAL_HEADER.size else "  (needs recovery on next open)"),
    ]
    tables_s, cols_s, idx_s = SYSTEM_SCHEMAS
    tables = list(raw.heap_rows(tables_s.first_page, tables_s.serializer))
    columns = list(raw.heap_rows(cols_s.first_page, cols_s.serializer))
    indexes = list(raw.heap_rows(idx_s.first_page, idx_s.serializer))
    types = Counter()
    owner_pages: Counter[int] = Counter()
    owner_rows: Counter[int] = Counter()
    bad = []
    for pid in range(raw.num_pages):
        data = raw.page(pid)
        if not verify_checksum(data):
            bad.append(pid)
            continue
        _lsn, ptype, owner, _nxt = read_common_header(data)
        types[ptype.name] += 1
        if ptype is not PageType.FREE:
            owner_pages[owner] += 1
        if ptype is PageType.HEAP:
            owner_rows[owner] += sum(1 for _ in HeapPage(data).records())
    lines += [f"Tables:            {len(tables)}", f"Indexes:           {len(indexes)}", ""]
    lines.append("Page types:")
    for name, n in sorted(types.items()):
        lines.append(f"  {name:<16} {n}")
    if bad:
        lines.append(f"  CORRUPT (bad checksum): {bad}")
    lines.append("")
    lines.append("Tables:")
    for tid, name, first in tables:
        ncols = sum(1 for c in columns if c[0] == tid)
        lines.append(f"  {name:<20} id={tid:<5} columns={ncols:<3} rows={owner_rows[tid]:<7} "
                     f"heap pages={owner_pages[tid]:<5} first page={first}")
    if indexes:
        lines.append("")
        lines.append("Indexes:")
        names = {t[0]: t[1] for t in tables}
        for iid, name, tid, col, meta, uniq, auto in indexes:
            kind = "unique" if uniq else "non-unique"
            origin = " (constraint)" if auto else ""
            lines.append(f"  {name:<20} on {names.get(tid, '?')}({col}) {kind}{origin}, "
                         f"pages={owner_pages[iid]}, meta page={meta}")
    return "\n".join(lines)


def describe_page(path: str, pid: int, show_bytes: bool = False) -> str:
    raw = RawFile(path)
    data = raw.page(pid)
    lsn, ptype, owner, nxt = read_common_header(data)
    crc = struct.unpack_from("<I", data, 8)[0]
    names = owner_names(raw)
    lines = [
        f"Page {pid} of {path}",
        f"  type:       {ptype.name}",
        f"  page LSN:   {lsn}",
        f"  checksum:   {crc:#010x} ({'OK' if verify_checksum(data) else 'MISMATCH'})",
        f"  owner:      {owner} ({names.get(owner, 'unknown')})",
        f"  next page:  {_fmt_pid(nxt)}",
    ]
    if ptype is PageType.HEAP:
        page = HeapPage(data)
        lines += [
            f"  slots:      {page.slot_count}",
            f"  free_end:   {page.free_end}",
            f"  free bytes: {page.total_free()} (contiguous {page.contiguous_free()})",
            "",
            "  Slot directory:",
        ]
        serializer = _serializer_for(raw, owner)
        for i in range(page.slot_count):
            off, length = SLOT.unpack_from(data, SLOT_DIR_OFFSET + i * 4)
            if not off:
                lines.append(f"    [{i:3}] (empty)")
                continue
            rec = bytes(data[off : off + length])
            decoded = ""
            if serializer is not None:
                decoded = f"  {serializer.decode(rec)}"
            lines.append(f"    [{i:3}] offset={off:<5} len={length:<4} {rec[:24].hex()}{'..' if length > 24 else ''}"
                         f"{decoded}")
    elif ptype is PageType.BTREE_META:
        root, code, _magic = _META.unpack_from(data, COMMON_HEADER_SIZE)
        lines += [f"  root page:  {root}", f"  key type:   {_CODE_TYPES[code].value}"]
    elif ptype in (PageType.BTREE_LEAF, PageType.BTREE_INTERNAL):
        key_type = _index_key_type(raw, owner)
        if key_type is not None:
            node = _decode_node(data, pid, key_type)
            lines.append(f"  entries:    {len(node.keys)}")
            for i, (key, rpid, slot) in enumerate(node.keys[:40]):
                child = f" -> child {node.children[i + 1]}" if not node.is_leaf else ""
                lines.append(f"    {key!r:<30} rid=({rpid},{slot}){child}")
            if not node.is_leaf:
                lines.insert(len(lines) - min(40, len(node.keys)), f"    (leftmost child {node.children[0]})")
            if len(node.keys) > 40:
                lines.append(f"    ... {len(node.keys) - 40} more")
    if show_bytes:
        lines += ["", "  Raw bytes (non-zero rows):"]
        for off in range(0, PAGE_SIZE, 32):
            chunk = data[off : off + 32]
            if any(chunk):
                lines.append(f"    {off:04x}: {chunk.hex(' ')}")
    return "\n".join(lines)


def _serializer_for(raw: RawFile, owner: int):
    """Rebuild a table's record serializer from the raw system_columns rows."""
    from pydb.storage.record import ColumnType, RecordSerializer

    for schema in SYSTEM_SCHEMAS:
        if schema.id == owner:
            return schema.serializer
    cols_s = SYSTEM_SCHEMAS[1]
    cols = sorted((r[1], r[3]) for r in raw.heap_rows(cols_s.first_page, cols_s.serializer) if r[0] == owner)
    return RecordSerializer([ColumnType(t) for _pos, t in cols]) if cols else None


def _index_key_type(raw: RawFile, index_id: int):
    for pid in range(raw.num_pages):
        data = raw.page(pid)
        _l, ptype, owner, _n = read_common_header(data)
        if ptype is PageType.BTREE_META and owner == index_id:
            return _CODE_TYPES[_META.unpack_from(data, COMMON_HEADER_SIZE)[1]]
    return None


def describe_wal(path: str, limit: int = 200) -> str:
    wal_path = path if path.endswith(".wal") else path + ".wal"
    if not os.path.exists(wal_path):
        return f"{wal_path} does not exist"
    wal = WriteAheadLog.__new__(WriteAheadLog)  # read without truncating a torn tail
    wal.path, wal.sync = wal_path, False
    wal._buffer, wal._buffered_bytes = [], 0
    with open(wal_path, "rb") as fh:
        wal._fh = fh
        base = WAL_HEADER.unpack(fh.read(WAL_HEADER.size))[3]
        records = [r for r, _ in wal._iter_file()]
    lines = [f"WAL {wal_path}: base LSN {base}, {len(records)} records"]
    lines += [r.describe() for r in records[:limit]]
    if len(records) > limit:
        lines.append(f"... {len(records) - limit} more")
    return "\n".join(lines)
