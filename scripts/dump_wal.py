"""List the records in a database's write-ahead log.

Usage:  python scripts/dump_wal.py data/pydb.db
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.inspector import describe_wal  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/dump_wal.py DATABASE_FILE")
    print(describe_wal(sys.argv[1]))
