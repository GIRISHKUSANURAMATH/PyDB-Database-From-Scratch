"""Decode one raw page of a PyDB database file.

Usage:  python scripts/dump_page.py data/pydb.db 4 [--bytes]
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb.inspector import describe_page  # noqa: E402

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--bytes"]
    if len(args) != 2:
        sys.exit("usage: python scripts/dump_page.py DATABASE_FILE PAGE_ID [--bytes]")
    print(describe_page(args[0], int(args[1]), show_bytes="--bytes" in sys.argv))
