from __future__ import annotations

import os

import pytest

from pydb import Database


@pytest.fixture
def db_path(tmp_path) -> str:
    return os.path.join(str(tmp_path), "test.db")


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    yield database
    if not database.closed:
        database.close()


@pytest.fixture
def users(db):
    """A small users/orders data set used by many SQL tests."""
    db.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, city TEXT);
        INSERT INTO users VALUES
            (1, 'Girish', 21, 'Bengaluru'),
            (2, 'Asha', 34, 'Mysuru'),
            (3, 'Ravi', 17, 'Bengaluru'),
            (4, 'Meena', 21, NULL),
            (5, 'Kiran', 45, 'Hubballi');
        CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER, amount FLOAT, item TEXT);
        INSERT INTO orders VALUES
            (10, 1, 250.0, 'book'),
            (11, 1, 99.5, 'pen'),
            (12, 2, 1200.0, 'phone'),
            (13, 5, 40.0, 'tea'),
            (14, 9, 10.0, 'ghost');
        """
    )
    return db
