"""PyDB demo: a tiny social network.

Run from the project root:

    python examples/social_network.py

Shows table creation with constraints, inserts, indexes, joins, aggregation,
transactions with COMMIT and ROLLBACK, constraint enforcement and EXPLAIN.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pydb import ConstraintViolationError, Database  # noqa: E402
from pydb.cli import format_table  # noqa: E402

DB_PATH = os.path.join(ROOT, "data", "social_network.db")


def show(db: Database, title: str, sql: str, params=()) -> None:
    print(f"\n### {title}\n{sql.strip()}")
    result = db.execute(sql, params)
    print(format_table(result.columns, result.rows))


def fresh_database() -> Database:
    for suffix in ("", ".wal", ".lock"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)  # only the demo's own files
    return Database(DB_PATH)


def main() -> None:
    db = fresh_database()
    print(f"Created {DB_PATH}")

    db.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            city TEXT,
            joined INTEGER DEFAULT 2024
        );
        CREATE TABLE posts (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT NOT NULL,
            likes INTEGER DEFAULT 0
        );
        CREATE TABLE comments (
            id INTEGER PRIMARY KEY,
            post_id INTEGER NOT NULL REFERENCES posts(id),
            user_id INTEGER NOT NULL REFERENCES users(id),
            body TEXT
        );
        """
    )
    print("Tables:", ", ".join(db.tables()))

    # ---------------------------------------------------------------- inserts (one transaction)
    users = [(1, "girish", "Bengaluru"), (2, "asha", "Mysuru"), (3, "ravi", "Bengaluru"),
             (4, "meena", "Hubballi"), (5, "kiran", "Mangaluru")]
    posts = [(10, 1, "Building a database from scratch", 42), (11, 1, "B+ trees explained", 31),
             (12, 2, "Weekend in Coorg", 12), (13, 3, "My first PR", 7), (14, 4, "Filter coffee", 25),
             (15, 2, "Learning SQL joins", 18)]
    comments = [(100, 10, 2, "Amazing!"), (101, 10, 3, "How does the WAL work?"), (102, 11, 4, "Great diagrams"),
                (103, 12, 1, "Beautiful"), (104, 10, 5, "Bookmarked"), (105, 15, 1, "Hash join next?")]
    with db.transaction():
        for u in users:
            db.execute("INSERT INTO users (id, username, city) VALUES (?, ?, ?)", u)
        for p in posts:
            db.execute("INSERT INTO posts VALUES (?, ?, ?, ?)", p)
        for c in comments:
            db.execute("INSERT INTO comments VALUES (?, ?, ?, ?)", c)
    print(f"Inserted {len(users)} users, {len(posts)} posts, {len(comments)} comments in one transaction.")

    # ---------------------------------------------------------------- indexes + EXPLAIN
    db.execute("CREATE INDEX idx_posts_user ON posts(user_id)")
    db.execute("CREATE INDEX idx_comments_post ON comments(post_id)")
    sql = "SELECT title, likes FROM posts WHERE user_id = 1"
    print(f"\n### EXPLAIN {sql}\n{db.explain(sql)}")
    show(db, "Posts by user 1 (index scan)", sql)

    # ---------------------------------------------------------------- joins
    show(db, "Posts with their authors (join)", """
        SELECT u.username, p.title, p.likes
        FROM posts p INNER JOIN users u ON u.id = p.user_id
        ORDER BY p.likes DESC
    """)
    show(db, "Comments with post title and commenter (3-way join)", """
        SELECT p.title, u.username AS commenter, c.body
        FROM comments c
        JOIN posts p ON p.id = c.post_id
        JOIN users u ON u.id = c.user_id
        ORDER BY c.id
    """)
    print("\n### Plan of the 3-way join")
    print(db.explain("SELECT p.title, u.username, c.body FROM comments c JOIN posts p ON p.id = c.post_id "
                     "JOIN users u ON u.id = c.user_id"))

    # ---------------------------------------------------------------- aggregation
    show(db, "Activity per user (LEFT JOIN + GROUP BY)", """
        SELECT u.username, COUNT(p.id) AS posts, SUM(p.likes) AS total_likes
        FROM users u LEFT JOIN posts p ON p.user_id = u.id
        GROUP BY u.username
        ORDER BY total_likes DESC
    """)
    show(db, "Cities with more than one user (HAVING)", """
        SELECT city, COUNT(*) AS members FROM users GROUP BY city HAVING COUNT(*) > 1
    """)

    # ---------------------------------------------------------------- transactions
    print("\n### Transaction with ROLLBACK")
    db.execute("BEGIN")
    db.execute("UPDATE posts SET likes = likes + 1000 WHERE user_id = 1")
    db.execute("DELETE FROM comments WHERE post_id = 10")
    inside = db.execute("SELECT SUM(likes) FROM posts").scalar()
    db.execute("ROLLBACK")
    after = db.execute("SELECT SUM(likes) FROM posts").scalar()
    n_comments = db.execute("SELECT COUNT(*) FROM comments WHERE post_id = 10").scalar()
    print(f"total likes inside the transaction: {inside}; after ROLLBACK: {after}; comments on post 10: {n_comments}")

    print("\n### Transaction with COMMIT")
    with db.transaction():
        db.execute("INSERT INTO posts VALUES (16, 5, 'Mangaluru buns recipe', 3)")
        db.execute("UPDATE users SET city = 'Udupi' WHERE username = 'kiran'")
    show(db, "Kiran after commit", "SELECT u.username, u.city, p.title FROM users u JOIN posts p "
                                   "ON p.user_id = u.id WHERE u.username = ?", ("kiran",))

    # ---------------------------------------------------------------- constraints
    print("\n### Constraints are enforced")
    for sql in [
        "INSERT INTO users (id, username) VALUES (6, 'girish')",          # UNIQUE
        "INSERT INTO posts (id, user_id, title) VALUES (20, 99, 'x')",    # FOREIGN KEY
        "INSERT INTO users (id, username) VALUES (1, 'someone')",         # PRIMARY KEY
        "DELETE FROM users WHERE id = 1",                                 # referenced by posts
    ]:
        try:
            db.execute(sql)
        except ConstraintViolationError as exc:
            print(f"  rejected: {sql}\n      -> {exc}")

    stats = db.stats()
    print(f"\nBuffer pool hit ratio: {stats['buffer_pool']['hit_ratio']:.1%}, pages in file: {stats['pages']}, "
          f"commits: {stats['transactions']['commits']}")
    db.close()
    print(f"\nDatabase closed cleanly. Inspect it with: python scripts/inspect_db.py {os.path.relpath(DB_PATH)}")


if __name__ == "__main__":
    main()
