"""Print the structure of a B+ tree index (opens the database through the engine).

Usage:  python scripts/dump_btree.py data/pydb.db idx_users_age
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydb import Database  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/dump_btree.py DATABASE_FILE INDEX_NAME")
    with Database(sys.argv[1]) as db:
        info = db.engine.catalog.get_index(sys.argv[2])
        tree = db.engine.indexes.tree(info)
        print(f"Index {info.name} on column {info.column}: {tree.stats()}")
        print(tree.dump())
