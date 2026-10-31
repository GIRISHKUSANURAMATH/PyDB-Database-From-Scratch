"""The smallest useful PyDB program — both API styles.

    python examples/quickstart.py
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydb import Database  # noqa: E402

path = os.path.join(ROOT, "data", "quickstart.db")
for suffix in ("", ".wal"):
    if os.path.exists(path + suffix):
        os.remove(path + suffix)

# 1. Convenience API (no SQL needed)
db = Database(path)
db.create_table("users", {"id": "INTEGER", "name": "TEXT", "age": "INTEGER"})
db.insert("users", {"id": 1, "name": "Girish", "age": 21})
print(db.select("users"))
db.close()

# 2. SQL API with safe parameter binding — data written above survived the reopen
db = Database(path)
db.execute("CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT, marks INTEGER)")
db.execute("INSERT INTO students VALUES (?, ?, ?)", (1, "Girish", 90))
db.execute("INSERT INTO students VALUES (?, ?, ?)", (2, "Asha", 72))
for row in db.execute("SELECT * FROM students WHERE marks >= ?", (80,)):
    print(row)
print(db.select("users"))
db.close()
