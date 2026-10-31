"""PyDB — an educational relational database engine built from scratch in Python.

Quick start::

    from pydb import Database

    db = Database("example.db")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    db.execute("INSERT INTO users VALUES (?, ?, ?)", (1, "Girish", 21))
    print(db.execute("SELECT * FROM users").rows)
    db.close()
"""

from pydb.config import EngineConfig
from pydb.database import Database
from pydb.errors import (
    ConstraintViolationError,
    DatabaseError,
    DeadlockError,
    ExecutionError,
    ParseError,
    TableNotFoundError,
    TransactionError,
)
from pydb.execution.executor import Result, Session

__version__ = "0.1.0"

__all__ = [
    "Database",
    "EngineConfig",
    "Result",
    "Session",
    "DatabaseError",
    "ParseError",
    "ExecutionError",
    "ConstraintViolationError",
    "TableNotFoundError",
    "TransactionError",
    "DeadlockError",
    "__version__",
]
