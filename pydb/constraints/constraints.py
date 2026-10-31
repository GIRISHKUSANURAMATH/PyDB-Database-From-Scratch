"""Integrity constraints: NOT NULL, DEFAULT, PRIMARY KEY, UNIQUE, FOREIGN KEY.

Constraints are checked *before* a row is written, inside the same
transaction, so a violation aborts only the current statement (the
transaction manager rolls back to the statement's savepoint).

* **PRIMARY KEY** = NOT NULL + UNIQUE, enforced through an automatically
  created unique B+ tree index (``pk_<table>``).
* **UNIQUE** uses an automatic index too (``uq_<table>_<column>``); the check
  is a single index probe instead of a table scan. NULLs never conflict.
* **FOREIGN KEY** ``REFERENCES parent(col)``: the parent column must be a
  PRIMARY KEY or UNIQUE column (so it is indexed). Inserting/updating a child
  requires the parent key to exist; deleting/updating a referenced parent row
  is rejected (``ON DELETE RESTRICT`` semantics).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from pydb.catalog.schema import Column, TableSchema
from pydb.errors import ConstraintViolationError
from pydb.storage.record import coerce_value

if TYPE_CHECKING:
    from pydb.catalog.catalog import Catalog
    from pydb.index.index_manager import IndexManager

RID = tuple[int, int]


def build_row(table: TableSchema, provided: dict[str, Any]) -> tuple:
    """Turn ``{column: value}`` into a full row tuple, applying DEFAULTs and type coercion."""
    row = []
    for col in table.columns:
        if col.name in provided:
            value = provided[col.name]
        elif col.has_default:
            value = col.default
        else:
            value = None
        row.append(coerce_value(value, col.type, col.name))
    return tuple(row)


def coerce_row(table: TableSchema, row: Iterable[Any]) -> tuple:
    return tuple(coerce_value(v, c.type, c.name) for v, c in zip(row, table.columns, strict=False))


def check_not_null(table: TableSchema, row: tuple) -> None:
    for col, value in zip(table.columns, row, strict=False):
        if value is None and (col.not_null or col.primary_key):
            kind = "PRIMARY KEY" if col.primary_key else "NOT NULL"
            raise ConstraintViolationError(f"{kind} constraint failed: {table.name}.{col.name} cannot be NULL")


def unique_columns(table: TableSchema) -> list[Column]:
    return [c for c in table.columns if c.primary_key or c.unique]


def check_unique(indexes: IndexManager, table: TableSchema, row: tuple, ignore_rid: RID | None = None) -> None:
    for col in unique_columns(table):
        value = row[table.position(col.name)]
        if value is None:
            continue
        info = table.index_on(col.name)
        if info is None:  # pragma: no cover - auto index always exists
            continue
        clashes = [rid for rid in indexes.lookup(info, value) if rid != ignore_rid]
        if clashes:
            kind = "PRIMARY KEY" if col.primary_key else "UNIQUE"
            raise ConstraintViolationError(
                f"{kind} constraint failed: duplicate value {value!r} for {table.name}.{col.name}"
            )


def check_foreign_keys(catalog: Catalog, indexes: IndexManager, table: TableSchema, row: tuple) -> None:
    for col in table.columns:
        if not col.references:
            continue
        value = row[table.position(col.name)]
        if value is None:
            continue  # NULL foreign keys are allowed (no reference)
        parent = catalog.get_table(col.references[0])
        info = parent.index_on(col.references[1])
        if info is None or not indexes.lookup(info, value):
            raise ConstraintViolationError(
                f"FOREIGN KEY constraint failed: {table.name}.{col.name}={value!r} "
                f"has no matching {parent.name}.{col.references[1]}"
            )


def referencing_columns(catalog: Catalog, parent: TableSchema) -> list[tuple[TableSchema, Column]]:
    out = []
    for child in catalog.tables.values():
        for col in child.columns:
            if col.references and col.references[0] == parent.name:
                out.append((child, col))
    return out


def check_not_referenced(
    catalog: Catalog,
    parent: TableSchema,
    old_row: tuple,
    new_row: tuple | None,
    child_has_value: Callable[[TableSchema, str, Any], bool],
) -> None:
    """Reject deleting/updating a parent key that child rows still reference."""
    for child, col in referencing_columns(catalog, parent):
        ref_col = col.references[1]
        old_value = old_row[parent.position(ref_col)]
        if old_value is None:
            continue
        if new_row is not None and new_row[parent.position(ref_col)] == old_value:
            continue  # key unchanged
        if child_has_value(child, col.name, old_value):
            raise ConstraintViolationError(
                f"FOREIGN KEY constraint failed: {parent.name}.{ref_col}={old_value!r} "
                f"is still referenced by {child.name}.{col.name}"
            )
