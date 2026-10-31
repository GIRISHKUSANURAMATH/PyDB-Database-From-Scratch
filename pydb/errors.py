"""Exception hierarchy for PyDB.

Every error raised deliberately by the engine derives from :class:`DatabaseError`,
so callers (and the CLI) can catch one type and show a clean message instead of
a raw traceback.
"""

from __future__ import annotations


class DatabaseError(Exception):
    """Base class for all PyDB errors."""


# --- storage -----------------------------------------------------------------
class StorageError(DatabaseError):
    """A problem in the storage layer (files, pages, records)."""


class PageError(StorageError):
    """A page is malformed or an operation on it is invalid."""


class PageCorruptionError(PageError):
    """A page failed its checksum — the bytes on disk are not what we wrote."""


class PageFullError(PageError):
    """A record does not fit into the page."""


class RecordTooLargeError(StorageError):
    """A record is larger than the maximum that fits in a single page."""


class BufferPoolFullError(StorageError):
    """Every frame in the buffer pool is pinned; nothing can be evicted."""


class DatabaseLockedError(StorageError):
    """Another process already has this database file open."""


# --- catalog -------------------------------------------------------------------
class CatalogError(DatabaseError):
    """A problem with schema metadata."""


class TableNotFoundError(CatalogError):
    pass


class TableAlreadyExistsError(CatalogError):
    pass


class ColumnNotFoundError(CatalogError):
    pass


class IndexNotFoundError(CatalogError):
    pass


class IndexAlreadyExistsError(CatalogError):
    pass


# --- SQL -----------------------------------------------------------------------
class ParseError(DatabaseError):
    """The SQL text could not be tokenized or parsed."""


class ExecutionError(DatabaseError):
    """A statement was valid SQL but could not be executed."""


class TypeMismatchError(ExecutionError):
    """A value does not match the declared column type."""


class ConstraintViolationError(ExecutionError):
    """PRIMARY KEY / UNIQUE / NOT NULL / FOREIGN KEY violated."""


# --- indexes -------------------------------------------------------------------
class IndexError(DatabaseError):  # noqa: A001 - intentional, namespaced as pydb.errors.IndexError
    """A problem inside an index structure."""


# --- transactions ----------------------------------------------------------------
class TransactionError(DatabaseError):
    """Invalid transaction usage (e.g. COMMIT without BEGIN)."""


class LockError(TransactionError):
    """A lock could not be granted."""


class DeadlockError(LockError):
    """Granting the lock would create a cycle in the wait-for graph."""


class LockTimeoutError(LockError):
    """The lock was not granted within the configured timeout."""


class RecoveryError(DatabaseError):
    """Crash recovery could not complete."""
