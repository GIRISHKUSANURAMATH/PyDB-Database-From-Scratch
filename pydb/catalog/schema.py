"""Schema objects: columns, tables and indexes as the engine sees them in memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydb.errors import ColumnNotFoundError
from pydb.storage.record import ColumnType, RecordSerializer


@dataclass
class Column:
    name: str
    type: ColumnType
    not_null: bool = False
    unique: bool = False
    primary_key: bool = False
    has_default: bool = False
    default: Any = None
    references: tuple[str, str] | None = None  # (table, column) for FOREIGN KEY

    def describe(self) -> str:
        parts = [self.name, self.type.value]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        else:
            if self.not_null:
                parts.append("NOT NULL")
            if self.unique:
                parts.append("UNIQUE")
        if self.has_default:
            parts.append(f"DEFAULT {_sql_literal(self.default)}")
        if self.references:
            parts.append(f"REFERENCES {self.references[0]}({self.references[1]})")
        return " ".join(parts)


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


@dataclass
class IndexInfo:
    id: int
    name: str
    table_id: int
    column: str
    meta_page: int
    unique: bool = False
    auto: bool = False  # created implicitly for PRIMARY KEY / UNIQUE


@dataclass
class TableSchema:
    id: int
    name: str
    first_page: int
    columns: list[Column]
    indexes: list[IndexInfo] = field(default_factory=list)
    system: bool = False

    def __post_init__(self) -> None:
        self._positions = {c.name: i for i, c in enumerate(self.columns)}
        self.serializer = RecordSerializer([c.type for c in self.columns])

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def has_column(self, name: str) -> bool:
        return name in self._positions

    def position(self, name: str) -> int:
        try:
            return self._positions[name]
        except KeyError:
            raise ColumnNotFoundError(f"table {self.name!r} has no column {name!r}") from None

    def column(self, name: str) -> Column:
        return self.columns[self.position(name)]

    @property
    def primary_key(self) -> Column | None:
        return next((c for c in self.columns if c.primary_key), None)

    def index_on(self, column: str) -> IndexInfo | None:
        """Best index for ``column`` (a unique one if several exist)."""
        candidates = [ix for ix in self.indexes if ix.column == column]
        candidates.sort(key=lambda ix: not ix.unique)
        return candidates[0] if candidates else None

    def ddl(self) -> str:
        cols = ",\n    ".join(c.describe() for c in self.columns)
        return f"CREATE TABLE {self.name} (\n    {cols}\n);"
