"""Engine-wide constants and tunable configuration.

Keeping these in one place avoids "magic numbers" scattered through the code
and makes the design decisions easy to find during a code review.
"""

from __future__ import annotations

from dataclasses import dataclass

# Page size: 4096 bytes matches the page size of most operating systems and the
# sector/block size of modern SSDs, so one database page maps onto one OS page.
# It is also the default in SQLite; PostgreSQL uses 8192.
PAGE_SIZE = 4096

# Sentinel for "no page" in on-disk pointers (u32 max).
INVALID_PAGE_ID = 0xFFFFFFFF

# File magic + format version written into page 0.
DB_MAGIC = b"PYDB"
DB_FORMAT_VERSION = 1
WAL_MAGIC = b"PWAL"

# Fixed page ids reserved at database creation.
HEADER_PAGE_ID = 0
SYSTEM_TABLES_PAGE_ID = 1
SYSTEM_COLUMNS_PAGE_ID = 2
SYSTEM_INDEXES_PAGE_ID = 3
FIRST_USER_OBJECT_ID = 100

# System catalog object ids (also used as page owner ids).
SYSTEM_TABLES_ID = 1
SYSTEM_COLUMNS_ID = 2
SYSTEM_INDEXES_ID = 3


@dataclass
class EngineConfig:
    """Runtime knobs. Defaults are sensible for an educational workload."""

    buffer_pool_size: int = 256  # pages (256 * 4 KiB = 1 MiB)
    # fsync the WAL on COMMIT. Turning this off trades durability for speed.
    sync_commit: bool = True
    # Checkpoint automatically when the WAL grows beyond this many bytes and no
    # transaction is active.
    checkpoint_wal_bytes: int = 8 * 1024 * 1024
    # Maximum entries per B+ tree node. ``None`` = limited only by page space.
    # Tests set a tiny value to force node splits with few rows.
    btree_max_entries: int | None = None
    lock_timeout: float = 5.0  # seconds to wait for a lock before giving up
