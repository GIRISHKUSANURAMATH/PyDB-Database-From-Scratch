"""Summarize a PyDB database file without opening it through the engine.

Usage:  python scripts/inspect_db.py data/pydb.db
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.inspector import describe_database  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/inspect_db.py DATABASE_FILE")
    print(describe_database(sys.argv[1]))
