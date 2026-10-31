"""Record (row) serialization.

A row is encoded against its table schema as::

    +-------------------+------------------------------------------+
    | null bitmap       | non-NULL values, in column order          |
    | ceil(n/8) bytes   |                                           |
    +-------------------+------------------------------------------+

Value encodings (little-endian):

=========  =================================================
INTEGER    8-byte signed (``<q``)
FLOAT      8-byte IEEE-754 double (``<d``)
BOOLEAN    1 byte (0/1)
TEXT       2-byte length + UTF-8 bytes (variable length)
=========  =================================================

NULL columns take zero bytes — only their bit in the bitmap is set. This is the
same idea PostgreSQL uses in its tuple header.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from enum import Enum
from typing import Any

from pydb.errors import RecordTooLargeError, TypeMismatchError
from pydb.storage.page import MAX_RECORD_SIZE

_INT = struct.Struct("<q")
_FLOAT = struct.Struct("<d")
_LEN = struct.Struct("<H")
INT64_MIN, INT64_MAX = -(2**63), 2**63 - 1


class ColumnType(str, Enum):
    INTEGER = "INTEGER"
    FLOAT = "FLOAT"
    TEXT = "TEXT"
    BOOLEAN = "BOOLEAN"

    @classmethod
    def parse(cls, name: str) -> ColumnType:
        aliases = {
            "INT": "INTEGER", "BIGINT": "INTEGER", "SMALLINT": "INTEGER",
            "REAL": "FLOAT", "DOUBLE": "FLOAT", "NUMERIC": "FLOAT", "DECIMAL": "FLOAT",
            "VARCHAR": "TEXT", "CHAR": "TEXT", "STRING": "TEXT",
            "BOOL": "BOOLEAN",
        }
        key = name.upper()
        key = aliases.get(key, key)
        try:
            return cls(key)
        except ValueError:
            raise TypeMismatchError(f"unknown column type {name!r}") from None


def coerce_value(value: Any, col_type: ColumnType, column: str = "?") -> Any:
    """Validate/convert a Python value for storage in a column of ``col_type``."""
    if value is None:
        return None
    if col_type is ColumnType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            else:
                raise TypeMismatchError(f"column {column!r} expects INTEGER, got {value!r}")
        if not INT64_MIN <= value <= INT64_MAX:
            raise TypeMismatchError(f"integer {value} out of 64-bit range for column {column!r}")
        return value
    if col_type is ColumnType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeMismatchError(f"column {column!r} expects FLOAT, got {value!r}")
        return float(value)
    if col_type is ColumnType.TEXT:
        if not isinstance(value, str):
            raise TypeMismatchError(f"column {column!r} expects TEXT, got {value!r}")
        return value
    if col_type is ColumnType.BOOLEAN:
        if not isinstance(value, bool):
            if isinstance(value, int) and value in (0, 1):
                return bool(value)
            raise TypeMismatchError(f"column {column!r} expects BOOLEAN, got {value!r}")
        return value
    raise TypeMismatchError(f"unsupported type {col_type}")  # pragma: no cover


class RecordSerializer:
    """Encodes/decodes rows for one fixed list of column types."""

    def __init__(self, types: Sequence[ColumnType]) -> None:
        self.types = list(types)
        self.bitmap_size = (len(self.types) + 7) // 8

    def encode(self, values: Sequence[Any]) -> bytes:
        if len(values) != len(self.types):
            raise TypeMismatchError(f"expected {len(self.types)} values, got {len(values)}")
        bitmap = bytearray(self.bitmap_size)
        parts: list[bytes] = []
        for i, (value, col_type) in enumerate(zip(values, self.types, strict=False)):
            if value is None:
                bitmap[i // 8] |= 1 << (i % 8)
            elif col_type is ColumnType.INTEGER:
                parts.append(_INT.pack(value))
            elif col_type is ColumnType.FLOAT:
                parts.append(_FLOAT.pack(value))
            elif col_type is ColumnType.BOOLEAN:
                parts.append(b"\x01" if value else b"\x00")
            else:
                raw = value.encode("utf-8")
                if len(raw) > 0xFFFF:
                    raise RecordTooLargeError("TEXT value longer than 65535 bytes")
                parts.append(_LEN.pack(len(raw)) + raw)
        record = bytes(bitmap) + b"".join(parts)
        if not record:
            record = b"\x00"  # zero-column tables still need a non-empty record
        if len(record) > MAX_RECORD_SIZE:
            raise RecordTooLargeError(
                f"row is {len(record)} bytes; the maximum is {MAX_RECORD_SIZE} (one page, no overflow pages)"
            )
        return record

    def decode(self, record: bytes) -> tuple:
        bitmap = record[: self.bitmap_size]
        pos = self.bitmap_size
        out: list[Any] = []
        for i, col_type in enumerate(self.types):
            if bitmap[i // 8] & (1 << (i % 8)):
                out.append(None)
            elif col_type is ColumnType.INTEGER:
                out.append(_INT.unpack_from(record, pos)[0])
                pos += 8
            elif col_type is ColumnType.FLOAT:
                out.append(_FLOAT.unpack_from(record, pos)[0])
                pos += 8
            elif col_type is ColumnType.BOOLEAN:
                out.append(record[pos] != 0)
                pos += 1
            else:
                (length,) = _LEN.unpack_from(record, pos)
                pos += 2
                out.append(record[pos : pos + length].decode("utf-8"))
                pos += length
        return tuple(out)
