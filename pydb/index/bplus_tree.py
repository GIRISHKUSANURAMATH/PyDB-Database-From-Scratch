"""A disk-based B+ tree, stored in buffer-pool pages.

Structure
---------
* **Meta page** (type BTREE_META) — fixed page id recorded in the catalog. Holds
  the current root page id, so a root split only rewrites the meta page and
  never the catalog (same trick as PostgreSQL's B-tree metapage).
* **Internal nodes** (type BTREE_INTERNAL) — ``n`` separator keys and ``n+1``
  child pointers. Child ``i`` holds every entry ``>= keys[i-1]`` and ``< keys[i]``.
* **Leaf nodes** (type BTREE_LEAF) — sorted entries ``(key, rid)``; each leaf
  points to its right sibling through the common header's ``next_page`` field,
  so a range scan walks leaves left-to-right without going back up the tree.

Duplicate keys
--------------
Non-unique indexes may contain the same key many times. Every entry is made
unique by comparing ``(key, page_id, slot)`` — the RID is a tie-breaker. This
keeps every algorithm (search, split, delete) identical for unique and
non-unique indexes. Lookups for ``key`` start at ``(key, -1, -1)``, which sorts
before every real entry with that key.

Splitting
---------
A node splits when it exceeds the page capacity (or ``max_entries``, which
tests set very low to force deep trees). Leaves *copy* the first key of the
right half up into the parent; internal nodes *move* the middle key up. When
the root splits, a new root is created and the meta page is updated — this is
the only way the tree grows taller, so all leaves stay at the same depth.

Deletion
--------
Entries are removed from leaves without merging/redistributing under-full
nodes ("lazy deletion"). Many production engines make the same choice because
merges are rare-benefit and complex under concurrency; the cost is that a tree
that shrinks a lot keeps its height. Documented in docs/indexing.md.

Page layout (after the 24-byte common header)::

    leaf:      n u16 | n x (key | page u32 | slot u16)
    internal:  n u16 | child0 u32 | n x (key | page u32 | slot u16 | child u32)
    meta:      root u32 | key_type u8 | magic "BTRE"

Keys are encoded according to the indexed column type (INTEGER/FLOAT as 8
bytes, BOOLEAN as 1 byte, TEXT as u16 length + UTF-8).
"""

from __future__ import annotations

import bisect
import struct
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from pydb.config import INVALID_PAGE_ID, PAGE_SIZE
from pydb.errors import IndexError as PyDBIndexError
from pydb.storage.buffer_pool import BufferPool
from pydb.storage.free_space import FreeSpaceManager
from pydb.storage.page import COMMON_HEADER_SIZE, Page, PageType
from pydb.storage.record import ColumnType

Entry = tuple[Any, int, int]  # (key, page_id, slot)
RID = tuple[int, int]

_U16 = struct.Struct("<H")
_U32 = struct.Struct("<I")
_RIDS = struct.Struct("<IH")
_META = struct.Struct("<IB4s")
_META_MAGIC = b"BTRE"
_TYPE_CODES = {ColumnType.INTEGER: 0, ColumnType.FLOAT: 1, ColumnType.TEXT: 2, ColumnType.BOOLEAN: 3}
_CODE_TYPES = {v: k for k, v in _TYPE_CODES.items()}

NODE_CAPACITY = PAGE_SIZE - COMMON_HEADER_SIZE - 2  # bytes available for entries
NODE_CACHE_SIZE = 1024  # decoded nodes kept per tree
MAX_TEXT_KEY_BYTES = 512  # guarantees a split always produces two nodes that fit


@dataclass
class Node:
    page_id: int
    is_leaf: bool
    keys: list[Entry] = field(default_factory=list)
    children: list[int] = field(default_factory=list)  # internal only
    next_leaf: int = INVALID_PAGE_ID  # leaf only


class BPlusTree:
    def __init__(
        self,
        pool: BufferPool,
        fsm: FreeSpaceManager,
        meta_page_id: int,
        owner_id: int,
        max_entries: int | None = None,
    ) -> None:
        self.pool = pool
        self.fsm = fsm
        self.meta_page_id = meta_page_id
        self.owner_id = owner_id
        self.max_entries = max_entries
        # Decoded-node cache. Decoding a 4 KiB node into Python tuples is the most
        # expensive step of every index operation, so recently used nodes are kept
        # in decoded form. Every change goes through _write(), which refreshes the
        # cache, so it can never disagree with the page. (The pages themselves still
        # live in the buffer pool and are what gets written to disk.)
        self._cache: OrderedDict[int, Node] = OrderedDict()
        root, code = self._read_meta()
        self.key_type = _CODE_TYPES[code]
        self._root = root

    # ================================================================== creation
    @classmethod
    def create(
        cls,
        pool: BufferPool,
        fsm: FreeSpaceManager,
        owner_id: int,
        key_type: ColumnType,
        lsn: int = 0,
        max_entries: int | None = None,
        meta_page_id: int | None = None,
    ) -> BPlusTree:
        """Allocate a meta page and an empty root leaf (reusing ``meta_page_id`` if given)."""
        meta_pid = meta_page_id if meta_page_id is not None else fsm.allocate_page()
        root_pid = fsm.allocate_page()
        with pool.pin_new(root_pid) as frame:
            _encode_node(frame.data, Node(root_pid, True), key_type, owner_id, lsn)
        with pool.pin_new(meta_pid) as frame:
            page = Page(frame.data)
            page.format(PageType.BTREE_META, owner_id, lsn)
            _META.pack_into(frame.data, COMMON_HEADER_SIZE, root_pid, _TYPE_CODES[key_type], _META_MAGIC)
        return cls(pool, fsm, meta_pid, owner_id, max_entries)

    def _read_meta(self) -> tuple[int, int]:
        with self.pool.pin(self.meta_page_id) as frame:
            if Page(frame.data).page_type is not PageType.BTREE_META:
                raise PyDBIndexError(f"page {self.meta_page_id} is not a B+ tree meta page")
            root, code, magic = _META.unpack_from(frame.data, COMMON_HEADER_SIZE)
        if magic != _META_MAGIC:
            raise PyDBIndexError("bad B+ tree meta page")
        return root, code

    def _set_root(self, root_pid: int, lsn: int) -> None:
        with self.pool.pin(self.meta_page_id) as frame:
            _META.pack_into(frame.data, COMMON_HEADER_SIZE, root_pid, _TYPE_CODES[self.key_type], _META_MAGIC)
            page = Page(frame.data)
            page.lsn = max(page.lsn, lsn)
            frame.dirty = True
        self._root = root_pid

    @property
    def root_page_id(self) -> int:
        return self._root

    # ================================================================== node I/O
    def _read(self, pid: int) -> Node:
        node = self._cache.get(pid)
        if node is not None:
            self._cache.move_to_end(pid)
            return node
        with self.pool.pin(pid) as frame:
            node = _decode_node(frame.data, pid, self.key_type)
        self._remember(node)
        return node

    def _remember(self, node: Node) -> None:
        self._cache[node.page_id] = node
        self._cache.move_to_end(node.page_id)
        if len(self._cache) > NODE_CACHE_SIZE:
            self._cache.popitem(last=False)

    def _write(self, node: Node, lsn: int) -> None:
        with self.pool.pin(node.page_id) as frame:
            _encode_node(frame.data, node, self.key_type, self.owner_id, max(lsn, Page(frame.data).lsn))
            frame.dirty = True
        self._remember(node)

    def _new_node(self, is_leaf: bool, lsn: int) -> Node:
        pid = self.fsm.allocate_page()
        node = Node(pid, is_leaf)
        with self.pool.pin_new(pid) as frame:
            _encode_node(frame.data, node, self.key_type, self.owner_id, lsn)
        self._remember(node)
        return node

    def _overflows(self, node: Node) -> bool:
        if self.max_entries is not None and len(node.keys) > self.max_entries:
            return True
        return _node_size(node, self.key_type) > NODE_CAPACITY

    # ================================================================== search
    def _find_leaf(self, target: Entry) -> Node:
        node = self._read(self._root)
        while not node.is_leaf:
            node = self._read(node.children[bisect.bisect_right(node.keys, target)])
        return node

    def range_entries(
        self, low: Any = None, high: Any = None, low_inclusive: bool = True, high_inclusive: bool = True
    ) -> Iterator[Entry]:
        """Yield entries with ``low <= key <= high`` in key order (bounds optional)."""
        if low is None:
            node = self._leftmost_leaf()
            start = 0
        else:
            target = (low, -1, -1)
            node = self._find_leaf(target)
            start = bisect.bisect_left(node.keys, target)
        while True:
            for entry in node.keys[start:]:
                key = entry[0]
                if low is not None and not low_inclusive and key == low:
                    continue
                if high is not None and (key > high or (not high_inclusive and key == high)):
                    return
                yield entry
            if node.next_leaf == INVALID_PAGE_ID:
                return
            node = self._read(node.next_leaf)
            start = 0

    def search(self, key: Any) -> list[RID]:
        """All RIDs whose key equals ``key``."""
        return [(e[1], e[2]) for e in self.range_entries(key, key)]

    def contains(self, key: Any) -> bool:
        return next(self.range_entries(key, key), None) is not None

    def _leftmost_leaf(self) -> Node:
        node = self._read(self._root)
        while not node.is_leaf:
            node = self._read(node.children[0])
        return node

    def __iter__(self) -> Iterator[Entry]:
        return self.range_entries()

    # ================================================================== insert
    def insert(self, key: Any, rid: RID, lsn: int = 0) -> None:
        if key is None:
            raise PyDBIndexError("NULL keys are not stored in the index")
        self._check_key(key)
        entry: Entry = (key, rid[0], rid[1])
        split = self._insert(self._root, entry, lsn)
        if split is not None:  # the root split: grow the tree by one level
            sep, right_pid = split
            new_root = self._new_node(is_leaf=False, lsn=lsn)
            new_root.keys = [sep]
            new_root.children = [self._root, right_pid]
            self._write(new_root, lsn)
            self._set_root(new_root.page_id, lsn)

    def _insert(self, pid: int, entry: Entry, lsn: int) -> tuple[Entry, int] | None:
        node = self._read(pid)
        if node.is_leaf:
            pos = bisect.bisect_left(node.keys, entry)
            if pos < len(node.keys) and node.keys[pos] == entry:
                return None  # exact (key, rid) already present
            node.keys.insert(pos, entry)
        else:
            idx = bisect.bisect_right(node.keys, entry)
            split = self._insert(node.children[idx], entry, lsn)
            if split is None:
                return None
            sep, right_pid = split
            node.keys.insert(idx, sep)
            node.children.insert(idx + 1, right_pid)
        if not self._overflows(node):
            self._write(node, lsn)
            return None
        return self._split(node, lsn)

    def _split(self, node: Node, lsn: int) -> tuple[Entry, int]:
        mid = self._split_point(node)
        right = self._new_node(node.is_leaf, lsn)
        if node.is_leaf:
            right.keys = node.keys[mid:]
            node.keys = node.keys[:mid]
            right.next_leaf = node.next_leaf  # keep the leaf chain intact
            node.next_leaf = right.page_id
            separator = right.keys[0]  # copy up
        else:
            separator = node.keys[mid]  # move up
            right.keys = node.keys[mid + 1 :]
            right.children = node.children[mid + 1 :]
            node.keys = node.keys[:mid]
            node.children = node.children[: mid + 1]
        self._write(right, lsn)
        self._write(node, lsn)
        return separator, right.page_id

    def _split_point(self, node: Node) -> int:
        n = len(node.keys)
        if self.key_type is not ColumnType.TEXT:
            return n // 2
        # Variable-length keys: split by bytes so both halves fit in a page.
        sizes = [_key_size(e[0], self.key_type) for e in node.keys]
        half, acc = sum(sizes) / 2, 0
        for i, s in enumerate(sizes):
            acc += s
            if acc >= half:
                return min(max(i, 1), n - 1)
        return n // 2  # pragma: no cover

    # ================================================================== delete
    def delete(self, key: Any, rid: RID, lsn: int = 0) -> bool:
        """Remove the entry ``(key, rid)``. Returns False if it was not present."""
        if key is None:
            return False
        entry: Entry = (key, rid[0], rid[1])
        leaf = self._find_leaf(entry)
        pos = bisect.bisect_left(leaf.keys, entry)
        if pos < len(leaf.keys) and leaf.keys[pos] == entry:
            del leaf.keys[pos]
            self._write(leaf, lsn)
            return True
        return False

    # ================================================================== maintenance / introspection
    def all_page_ids(self) -> list[int]:
        pids, stack = [self.meta_page_id], [self._root]
        while stack:
            pid = stack.pop()
            pids.append(pid)
            node = self._read(pid)
            if not node.is_leaf:
                stack.extend(node.children)
        return pids

    def height(self) -> int:
        h, node = 1, self._read(self._root)
        while not node.is_leaf:
            h += 1
            node = self._read(node.children[0])
        return h

    def stats(self) -> dict[str, int]:
        leaves = internals = entries = 0
        stack = [self._root]
        while stack:
            node = self._read(stack.pop())
            if node.is_leaf:
                leaves += 1
                entries += len(node.keys)
            else:
                internals += 1
                stack.extend(node.children)
        return {"height": self.height(), "leaf_nodes": leaves, "internal_nodes": internals, "entries": entries}

    def check_invariants(self) -> None:
        """Verify ordering, separator bounds, uniform leaf depth and the leaf chain."""
        leaf_depths: set[int] = set()
        leaves_in_order: list[int] = []

        def walk(pid: int, lo: Entry | None, hi: Entry | None, depth: int) -> None:
            node = self._read(pid)
            if node.keys != sorted(node.keys):
                raise PyDBIndexError(f"node {pid} keys out of order")
            for k in node.keys:
                if (lo is not None and k < lo) or (hi is not None and k >= hi):
                    raise PyDBIndexError(f"node {pid} key {k} outside separator bounds")
            if node.is_leaf:
                leaf_depths.add(depth)
                leaves_in_order.append(pid)
                return
            if len(node.children) != len(node.keys) + 1:
                raise PyDBIndexError(f"internal node {pid} has wrong child count")
            bounds = [lo, *node.keys, hi]
            for i, child in enumerate(node.children):
                walk(child, bounds[i], bounds[i + 1], depth + 1)

        walk(self._root, None, None, 1)
        if len(leaf_depths) > 1:
            raise PyDBIndexError(f"leaves at different depths: {leaf_depths}")
        chain, pid = [], self._leftmost_leaf().page_id
        while pid != INVALID_PAGE_ID:
            chain.append(pid)
            pid = self._read(pid).next_leaf
        if chain != leaves_in_order:
            raise PyDBIndexError("leaf sibling chain does not match tree order")

    def _check_key(self, key: Any) -> None:
        if self.key_type is ColumnType.TEXT and len(key.encode("utf-8")) > MAX_TEXT_KEY_BYTES:
            raise PyDBIndexError(f"index key longer than {MAX_TEXT_KEY_BYTES} bytes")

    def dump(self) -> str:
        """Human-readable tree, used by scripts/dump_btree.py and the docs."""
        lines: list[str] = []

        def walk(pid: int, depth: int) -> None:
            node = self._read(pid)
            pad = "    " * depth
            if node.is_leaf:
                keys = [e[0] for e in node.keys]
                shown = keys if len(keys) <= 8 else [*keys[:4], "...", *keys[-3:]]
                lines.append(f"{pad}Leaf[page {pid}] {len(keys)} keys: {shown} -> next {_fmt_pid(node.next_leaf)}")
            else:
                lines.append(f"{pad}Internal[page {pid}] separators: {[e[0] for e in node.keys]}")
                for child in node.children:
                    walk(child, depth + 1)

        walk(self._root, 0)
        return "\n".join(lines)


def _fmt_pid(pid: int) -> str:
    return "-" if pid == INVALID_PAGE_ID else str(pid)


# ====================================================================== encoding
def _key_size(key: Any, key_type: ColumnType) -> int:
    if key_type is ColumnType.TEXT:
        return 2 + len(key.encode("utf-8"))
    if key_type is ColumnType.BOOLEAN:
        return 1
    return 8


def _node_size(node: Node, key_type: ColumnType) -> int:
    per_entry_extra = _RIDS.size + (0 if node.is_leaf else 4)
    if key_type is ColumnType.TEXT:
        keys = sum(_key_size(e[0], key_type) for e in node.keys)
    else:
        keys = _key_size(None, key_type) * len(node.keys)
    return keys + per_entry_extra * len(node.keys) + (0 if node.is_leaf else 4)


def _encode_key(key: Any, key_type: ColumnType) -> bytes:
    if key_type is ColumnType.INTEGER:
        return struct.pack("<q", key)
    if key_type is ColumnType.FLOAT:
        return struct.pack("<d", key)
    if key_type is ColumnType.BOOLEAN:
        return b"\x01" if key else b"\x00"
    raw = key.encode("utf-8")
    return _U16.pack(len(raw)) + raw


def _decode_key(buf: bytearray, pos: int, key_type: ColumnType) -> tuple[Any, int]:
    if key_type is ColumnType.INTEGER:
        return struct.unpack_from("<q", buf, pos)[0], pos + 8
    if key_type is ColumnType.FLOAT:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if key_type is ColumnType.BOOLEAN:
        return buf[pos] != 0, pos + 1
    (n,) = _U16.unpack_from(buf, pos)
    return bytes(buf[pos + 2 : pos + 2 + n]).decode("utf-8"), pos + 2 + n


def _encode_node(data: bytearray, node: Node, key_type: ColumnType, owner: int, lsn: int) -> None:
    page = Page(data)
    page.format(PageType.BTREE_LEAF if node.is_leaf else PageType.BTREE_INTERNAL, owner, lsn)
    parts = [_U16.pack(len(node.keys))]
    if node.is_leaf:
        page.next_page = node.next_leaf
        for key, pid, slot in node.keys:
            parts.append(_encode_key(key, key_type) + _RIDS.pack(pid, slot))
    else:
        parts.append(_U32.pack(node.children[0] if node.children else INVALID_PAGE_ID))
        for (key, pid, slot), child in zip(node.keys, node.children[1:], strict=False):
            parts.append(_encode_key(key, key_type) + _RIDS.pack(pid, slot) + _U32.pack(child))
    body = b"".join(parts)
    if COMMON_HEADER_SIZE + len(body) > PAGE_SIZE:  # pragma: no cover - split logic prevents this
        raise PyDBIndexError("B+ tree node overflow")
    data[COMMON_HEADER_SIZE : COMMON_HEADER_SIZE + len(body)] = body


def _decode_node(data: bytearray, pid: int, key_type: ColumnType) -> Node:
    ptype = Page(data).page_type
    if ptype not in (PageType.BTREE_LEAF, PageType.BTREE_INTERNAL):
        raise PyDBIndexError(f"page {pid} is not a B+ tree node (type {ptype.name})")
    node = Node(pid, ptype is PageType.BTREE_LEAF)
    pos = COMMON_HEADER_SIZE
    (n,) = _U16.unpack_from(data, pos)
    pos += 2
    if node.is_leaf:
        node.next_leaf = Page(data).next_page
        for _ in range(n):
            key, pos = _decode_key(data, pos, key_type)
            rpid, slot = _RIDS.unpack_from(data, pos)
            pos += _RIDS.size
            node.keys.append((key, rpid, slot))
    else:
        node.children.append(_U32.unpack_from(data, pos)[0])
        pos += 4
        for _ in range(n):
            key, pos = _decode_key(data, pos, key_type)
            rpid, slot = _RIDS.unpack_from(data, pos)
            pos += _RIDS.size
            node.keys.append((key, rpid, slot))
            node.children.append(_U32.unpack_from(data, pos)[0])
            pos += 4
    return node
