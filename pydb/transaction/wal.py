"""Write-Ahead Log (WAL).

The rule: **a change is described in the log before the changed page can reach
the data file**, and a transaction is committed exactly when its COMMIT record
is durable (fsync'd) in the log. Data pages themselves can then be written
lazily, in any order, at any time — after a crash the log is replayed.

File layout (``<database>.wal``)::

    +--------------------------------------------------------------+
    | file header: magic "PWAL" | version u16 | pad u16 | base_lsn u64 |  16 bytes
    +--------------------------------------------------------------+
    | record | record | record | ...                  (append-only) |
    +--------------------------------------------------------------+

Record layout::

    length u32 | crc32 u32 | lsn u64 | txn_id u64 | type u8 | flags u8 | payload

* ``length`` is the full record length, ``crc32`` covers everything after the
  CRC field. A torn/partial record at the tail (crash in the middle of a
  write) fails the CRC and is discarded, together with everything after it.
* ``lsn`` (log sequence number) increases by one per record and never
  restarts — ``base_lsn`` in the header carries it across truncations.
* ``flags & FLAG_CLR`` marks a *compensation log record*: the redo record of
  an undo performed during ROLLBACK. Recovery redoes CLRs but never undoes them.

Payloads:

=========  ==============================================================
BEGIN      (empty)
COMMIT     (empty)
ABORT      (empty)
INSERT     owner u32, page u32, slot u16, len u32, after-image
DELETE     owner u32, page u32, slot u16, len u32, before-image
UPDATE     owner u32, page u32, slot u16, len u32, before, len u32, after
NEW_PAGE   owner u32, page u32, prev_page u32
=========  ==============================================================

Records carry **physical** page/slot addresses plus full before/after images,
so redo and undo are simple "set slot to bytes" operations (physiological
logging in ARIES terminology).
"""

from __future__ import annotations

import logging
import os
import struct
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import IntEnum
from typing import BinaryIO

from pydb.config import INVALID_PAGE_ID, WAL_MAGIC

log = logging.getLogger(__name__)

WAL_HEADER = struct.Struct("<4sHHQ")
WAL_VERSION = 1
REC_HEADER = struct.Struct("<IIQQBB")  # length, crc, lsn, txn, type, flags
_ADDR = struct.Struct("<IIH")  # owner, page, slot
_LEN = struct.Struct("<I")
_NEWPAGE = struct.Struct("<III")

FLAG_CLR = 0x01


class LogType(IntEnum):
    BEGIN = 1
    COMMIT = 2
    ABORT = 3
    INSERT = 4
    DELETE = 5
    UPDATE = 6
    NEW_PAGE = 7


@dataclass
class LogRecord:
    type: LogType
    txn_id: int
    lsn: int = 0
    flags: int = 0
    owner: int = 0
    page_id: int = INVALID_PAGE_ID
    slot: int = 0
    before: bytes = b""
    after: bytes = b""
    prev_page: int = INVALID_PAGE_ID

    @property
    def is_clr(self) -> bool:
        return bool(self.flags & FLAG_CLR)

    @property
    def rid(self) -> tuple[int, int]:
        return (self.page_id, self.slot)

    # -------------------------------------------------------------- (de)serialize
    def _payload(self) -> bytes:
        t = self.type
        if t in (LogType.BEGIN, LogType.COMMIT, LogType.ABORT):
            return b""
        if t is LogType.NEW_PAGE:
            return _NEWPAGE.pack(self.owner, self.page_id, self.prev_page)
        addr = _ADDR.pack(self.owner, self.page_id, self.slot)
        if t is LogType.INSERT:
            return addr + _LEN.pack(len(self.after)) + self.after
        if t is LogType.DELETE:
            return addr + _LEN.pack(len(self.before)) + self.before
        if t is LogType.UPDATE:
            return addr + _LEN.pack(len(self.before)) + self.before + _LEN.pack(len(self.after)) + self.after
        raise ValueError(f"unknown log type {t}")  # pragma: no cover

    def encode(self) -> bytes:
        payload = self._payload()
        length = REC_HEADER.size + len(payload)
        body = struct.pack("<QQBB", self.lsn, self.txn_id, int(self.type), self.flags) + payload
        crc = zlib.crc32(body)
        return struct.pack("<II", length, crc) + body

    @classmethod
    def decode(cls, buf: bytes) -> LogRecord:
        _length, _crc, lsn, txn, typ, flags = REC_HEADER.unpack_from(buf, 0)
        rec = cls(LogType(typ), txn, lsn, flags)
        p = REC_HEADER.size
        t = rec.type
        if t is LogType.NEW_PAGE:
            rec.owner, rec.page_id, rec.prev_page = _NEWPAGE.unpack_from(buf, p)
        elif t in (LogType.INSERT, LogType.DELETE, LogType.UPDATE):
            rec.owner, rec.page_id, rec.slot = _ADDR.unpack_from(buf, p)
            p += _ADDR.size
            (n,) = _LEN.unpack_from(buf, p)
            first = bytes(buf[p + 4 : p + 4 + n])
            p += 4 + n
            if t is LogType.INSERT:
                rec.after = first
            elif t is LogType.DELETE:
                rec.before = first
            else:
                rec.before = first
                (m,) = _LEN.unpack_from(buf, p)
                rec.after = bytes(buf[p + 4 : p + 4 + m])
        return rec

    def describe(self) -> str:
        clr = " CLR" if self.is_clr else ""
        base = f"LSN {self.lsn:>6}  T{self.txn_id:<4} {self.type.name}{clr}"
        if self.type is LogType.NEW_PAGE:
            return f"{base} owner={self.owner} page={self.page_id} prev={self.prev_page}"
        if self.type in (LogType.INSERT, LogType.DELETE, LogType.UPDATE):
            return (f"{base} owner={self.owner} rid=({self.page_id},{self.slot}) "
                    f"before={len(self.before)}B after={len(self.after)}B")
        return base


class WriteAheadLog:
    """Append-only log file with group-buffered writes and explicit flush."""

    def __init__(self, path: str, min_next_lsn: int = 1, sync: bool = True) -> None:
        self.path = path
        self.sync = sync
        exists = os.path.exists(path) and os.path.getsize(path) >= WAL_HEADER.size
        self._fh: BinaryIO = open(path, "r+b" if exists else "w+b")
        self._buffer: list[bytes] = []
        self._buffered_bytes = 0
        if exists:
            base = self._read_header()
            last_lsn, end = self._scan_valid_end()
            self._fh.truncate(end)  # drop a torn tail, if any
            self.next_lsn = max(base, last_lsn + 1, min_next_lsn)
        else:
            self.next_lsn = max(1, min_next_lsn)
            self._write_header(self.next_lsn)
        self.flushed_lsn = self.next_lsn - 1
        self.flush_count = 0

    # ------------------------------------------------------------------ header
    def _write_header(self, base_lsn: int) -> None:
        self._fh.seek(0)
        self._fh.truncate(0)
        self._fh.write(WAL_HEADER.pack(WAL_MAGIC, WAL_VERSION, 0, base_lsn))
        self._fh.flush()
        if self.sync:
            os.fsync(self._fh.fileno())

    def _read_header(self) -> int:
        self._fh.seek(0)
        magic, version, _pad, base = WAL_HEADER.unpack(self._fh.read(WAL_HEADER.size))
        if magic != WAL_MAGIC or version != WAL_VERSION:
            from pydb.errors import RecoveryError

            raise RecoveryError(f"{self.path} is not a PyDB write-ahead log")
        return base

    def _scan_valid_end(self) -> tuple[int, int]:
        last_lsn, end = 0, WAL_HEADER.size
        for rec, rec_end in self._iter_file():
            last_lsn, end = rec.lsn, rec_end
        return last_lsn, end

    def _iter_file(self) -> Iterator[tuple[LogRecord, int]]:
        self._fh.seek(0, os.SEEK_END)
        size = self._fh.tell()
        pos = WAL_HEADER.size
        self._fh.seek(pos)
        data = self._fh.read(size - pos)
        off = 0
        while off + 8 <= len(data):
            length, crc = struct.unpack_from("<II", data, off)
            if length < REC_HEADER.size or off + length > len(data):
                break  # torn record at the tail
            body = data[off + 8 : off + length]
            if zlib.crc32(body) != crc:
                log.warning("WAL: checksum mismatch at offset %d; ignoring the rest of the log", pos + off)
                break
            yield LogRecord.decode(data[off : off + length]), pos + off + length
            off += length

    # ------------------------------------------------------------------ appending
    def append(self, rec: LogRecord) -> int:
        """Assign an LSN and buffer the record in memory. Returns the LSN."""
        rec.lsn = self.next_lsn
        self.next_lsn += 1
        raw = rec.encode()
        self._buffer.append(raw)
        self._buffered_bytes += len(raw)
        return rec.lsn

    def flush(self) -> None:
        """Write buffered records and fsync — after this they survive a crash."""
        if self._buffer:
            self._fh.seek(0, os.SEEK_END)
            self._fh.write(b"".join(self._buffer))
            self._buffer.clear()
            self._buffered_bytes = 0
            self._fh.flush()
            if self.sync:
                os.fsync(self._fh.fileno())
            self.flush_count += 1
        self.flushed_lsn = self.next_lsn - 1

    def flush_to(self, lsn: int) -> None:
        if lsn > self.flushed_lsn:
            self.flush()

    # ------------------------------------------------------------------ reading / maintenance
    def records(self) -> list[LogRecord]:
        """All durable records currently in the log file, in LSN order."""
        self.flush()
        return [rec for rec, _ in self._iter_file()]

    def truncate(self) -> None:
        """Discard all records (only safe right after a checkpoint)."""
        self.flush()
        self._write_header(self.next_lsn)
        self.flushed_lsn = self.next_lsn - 1

    def size(self) -> int:
        self._fh.seek(0, os.SEEK_END)
        return self._fh.tell() + self._buffered_bytes

    def discard_buffer(self) -> None:
        """Lose un-flushed records (used to simulate a crash)."""
        self._buffer.clear()
        self._buffered_bytes = 0

    def close(self) -> None:
        if not self._fh.closed:
            self.flush()
            self._fh.close()
