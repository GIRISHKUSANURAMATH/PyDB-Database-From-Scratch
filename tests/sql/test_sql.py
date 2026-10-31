"""End-to-end SQL tests through the public API."""

from __future__ import annotations

import pytest

from pydb.errors import (
    ColumnNotFoundError,
    ExecutionError,
    ParseError,
    TableAlreadyExistsError,
    TableNotFoundError,
    TypeMismatchError,
)


def rows(db, sql, params=()):
    return db.execute(sql, params).rows


# ----------------------------------------------------------------------------- DDL
def test_create_and_drop_table(db):
    db.execute("CREATE TABLE t (a INTEGER, b TEXT)")
    assert db.tables() == ["t"]
    with pytest.raises(TableAlreadyExistsError):
        db.execute("CREATE TABLE t (a INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS t (a INTEGER)")
    db.execute("DROP TABLE t")
    assert db.tables() == []
    with pytest.raises(TableNotFoundError):
        db.execute("SELECT * FROM t")
    db.execute("DROP TABLE IF EXISTS t")


def test_drop_table_reclaims_pages(db):
    db.execute("CREATE TABLE big (id INTEGER PRIMARY KEY, pad TEXT)")
    with db.transaction():
        for i in range(500):
            db.execute("INSERT INTO big VALUES (?, ?)", (i, "x" * 100))
    pages_before = db.stats()["pages"]
    db.execute("DROP TABLE big")
    assert db.stats()["free_pages"] > 10
    db.execute("CREATE TABLE big2 (id INTEGER PRIMARY KEY, pad TEXT)")
    with db.transaction():
        for i in range(500):
            db.execute("INSERT INTO big2 VALUES (?, ?)", (i, "y" * 100))
    assert db.stats()["pages"] <= pages_before + 2  # freed pages were reused


def test_type_aliases_and_varchar_length(db):
    db.execute("CREATE TABLE t (a INT, b VARCHAR(20), c REAL, d BOOL)")
    db.execute("INSERT INTO t VALUES (1, 'x', 2, TRUE)")
    assert rows(db, "SELECT * FROM t") == [(1, "x", 2.0, True)]


# ----------------------------------------------------------------------------- INSERT / SELECT
def test_insert_select_star(users):
    assert len(rows(users, "SELECT * FROM users")) == 5
    result = users.execute("SELECT * FROM users WHERE id = 1")
    assert result.columns == ["id", "name", "age", "city"]
    assert result.rows == [(1, "Girish", 21, "Bengaluru")]


def test_insert_with_column_list_and_defaults(db):
    db.execute("CREATE TABLE t (id INTEGER, status TEXT DEFAULT 'new', score FLOAT DEFAULT 1.5)")
    db.execute("INSERT INTO t (id) VALUES (1)")
    db.execute("INSERT INTO t (status, id) VALUES ('done', 2)")
    assert rows(db, "SELECT * FROM t ORDER BY id") == [(1, "new", 1.5), (2, "done", 1.5)]


def test_insert_errors(users):
    with pytest.raises(ExecutionError):
        users.execute("INSERT INTO users VALUES (6, 'x')")
    with pytest.raises(ColumnNotFoundError):
        users.execute("INSERT INTO users (id, nope) VALUES (6, 1)")
    with pytest.raises(TypeMismatchError):
        users.execute("INSERT INTO users VALUES ('six', 'x', 1, 'y')")


def test_projection_expressions_and_aliases(users):
    result = users.execute("SELECT name, age + 1 AS next_age, age * 2 FROM users WHERE id = 2")
    assert result.columns == ["name", "next_age", "age * 2"]
    assert result.rows == [("Asha", 35, 68)]


def test_select_without_from(db):
    assert rows(db, "SELECT 1 + 2, 'x', 7 / 2, 7.0 / 2, -7 / 2, 7 % 3, 1 / 0") == [
        (3, "x", 3, 3.5, -3, 1, None)
    ]


# ----------------------------------------------------------------------------- WHERE / expressions
@pytest.mark.parametrize(
    "where, expected",
    [
        ("age = 21", {1, 4}),
        ("age != 21", {2, 3, 5}),
        ("age <> 21", {2, 3, 5}),
        ("age < 21", {3}),
        ("age <= 21", {1, 3, 4}),
        ("age > 21", {2, 5}),
        ("age >= 34", {2, 5}),
        ("age >= 18 AND city = 'Bengaluru'", {1}),
        ("age < 18 OR age > 40", {3, 5}),
        ("NOT age = 21", {2, 3, 5}),
        ("city IS NULL", {4}),
        ("city IS NOT NULL", {1, 2, 3, 5}),
        ("age IN (17, 45)", {3, 5}),
        ("age NOT IN (17, 45)", {1, 2, 4}),
        ("age BETWEEN 20 AND 34", {1, 2, 4}),
        ("age NOT BETWEEN 20 AND 34", {3, 5}),
        ("name LIKE 'R%'", {3}),
        ("name LIKE '%a'", {2, 4}),
        ("name LIKE '_ira_'", {5}),
        ("name NOT LIKE '%i%'", {2, 4}),
        ("(age = 21 OR age = 17) AND city = 'Bengaluru'", {1, 3}),
        ("UPPER(city) = 'MYSURU'", {2}),
        ("LENGTH(name) = 4", {2, 3}),
    ],
)
def test_where_predicates(users, where, expected):
    assert {r[0] for r in rows(users, f"SELECT id FROM users WHERE {where}")} == expected


def test_three_valued_logic(users):
    # city = NULL is never true (unknown) — only IS NULL finds the NULL row
    assert rows(users, "SELECT id FROM users WHERE city = NULL") == []
    # NOT (unknown) is unknown -> excluded
    assert {r[0] for r in rows(users, "SELECT id FROM users WHERE NOT city = 'Mysuru'")} == {1, 3, 5}
    assert rows(db_value := users, "SELECT NULL AND FALSE, NULL OR TRUE, NULL AND TRUE")[0] == (False, True, None)
    assert db_value is users


def test_literals(db):
    assert rows(db, "SELECT TRUE, FALSE, NULL, 3.25, 'it''s', 1e3") == [(True, False, None, 3.25, "it's", 1000.0)]


def test_comparing_incompatible_types_errors(users):
    with pytest.raises(ExecutionError):
        rows(users, "SELECT * FROM users WHERE name > 5")


# ----------------------------------------------------------------------------- UPDATE / DELETE
def test_update(users):
    r = users.execute("UPDATE users SET age = age + 1, city = 'Delhi' WHERE age = 21")
    assert r.rowcount == 2
    assert rows(users, "SELECT id, age, city FROM users WHERE city = 'Delhi' ORDER BY id") == [
        (1, 22, "Delhi"), (4, 22, "Delhi")
    ]
    assert users.execute("UPDATE users SET age = 0 WHERE id = 999").rowcount == 0


def test_update_grows_row_beyond_page(db):
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, pad TEXT)")
    with db.transaction():
        for i in range(30):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, "a" * 120))
    db.execute("UPDATE t SET pad = ? WHERE id < 10", ("b" * 1500,))  # rows must move pages
    assert rows(db, "SELECT COUNT(*) FROM t WHERE pad = ?", ("b" * 1500,)) == [(10,)]
    assert rows(db, "SELECT id FROM t WHERE id = 3") == [(3,)]  # PK index follows moved rows
    assert len(rows(db, "SELECT * FROM t")) == 30


def test_delete(users):
    assert users.execute("DELETE FROM users WHERE age < 21").rowcount == 1
    assert {r[0] for r in rows(users, "SELECT id FROM users")} == {1, 2, 4, 5}
    assert users.execute("DELETE FROM users").rowcount == 4
    assert rows(users, "SELECT * FROM users") == []


# ----------------------------------------------------------------------------- ORDER BY / LIMIT / DISTINCT
def test_order_by(users):
    assert [r[0] for r in rows(users, "SELECT id FROM users ORDER BY age DESC, id")] == [5, 2, 1, 4, 3]
    assert [r[0] for r in rows(users, "SELECT id FROM users ORDER BY age, id DESC")] == [3, 4, 1, 2, 5]
    # NULLs sort first ascending
    assert rows(users, "SELECT city FROM users ORDER BY city")[0] == (None,)
    # by alias and by position
    assert [r[0] for r in rows(users, "SELECT name AS n FROM users ORDER BY n")] == [
        "Asha", "Girish", "Kiran", "Meena", "Ravi"
    ]
    assert [r[1] for r in rows(users, "SELECT id, name FROM users ORDER BY 2 DESC LIMIT 2")] == ["Ravi", "Meena"]


def test_limit_offset(users):
    assert [r[0] for r in rows(users, "SELECT id FROM users ORDER BY id LIMIT 2")] == [1, 2]
    assert [r[0] for r in rows(users, "SELECT id FROM users ORDER BY id LIMIT 2 OFFSET 3")] == [4, 5]
    assert rows(users, "SELECT id FROM users LIMIT 0") == []
    with pytest.raises(ExecutionError):
        rows(users, "SELECT id FROM users LIMIT -1")


def test_distinct(users):
    assert sorted(rows(users, "SELECT DISTINCT age FROM users")) == [(17,), (21,), (34,), (45,)]


# ----------------------------------------------------------------------------- aggregates
def test_aggregates(users):
    assert rows(users, "SELECT COUNT(*) FROM users") == [(5,)]
    assert rows(users, "SELECT COUNT(city), COUNT(DISTINCT city) FROM users") == [(4, 3)]
    assert rows(users, "SELECT SUM(age), MIN(age), MAX(age), AVG(age) FROM users") == [(138, 17, 45, 27.6)]
    assert rows(users, "SELECT MIN(name), MAX(name) FROM users") == [("Asha", "Ravi")]


def test_aggregates_on_empty_table(db):
    db.execute("CREATE TABLE e (x INTEGER)")
    assert rows(db, "SELECT COUNT(*), SUM(x), AVG(x), MIN(x) FROM e") == [(0, None, None, None)]
    assert rows(db, "SELECT x, COUNT(*) FROM e GROUP BY x") == []


def test_group_by(users):
    result = users.execute("SELECT age, COUNT(*) FROM users GROUP BY age ORDER BY age")
    assert result.columns == ["age", "COUNT(*)"]
    assert result.rows == [(17, 1), (21, 2), (34, 1), (45, 1)]
    assert rows(users, "SELECT city, COUNT(*) AS n FROM users GROUP BY city ORDER BY n DESC, city LIMIT 1") == [
        ("Bengaluru", 2)
    ]


def test_group_by_having(users):
    assert rows(users, "SELECT age, COUNT(*) FROM users GROUP BY age HAVING COUNT(*) > 1") == [(21, 2)]
    assert rows(
        users, "SELECT user_id, SUM(amount) FROM orders GROUP BY user_id HAVING SUM(amount) >= 100 ORDER BY user_id"
    ) == [(1, 349.5), (2, 1200.0)]


def test_group_by_expression_and_arithmetic_on_aggregates(users):
    assert rows(users, "SELECT age / 10, COUNT(*) * 10 FROM users GROUP BY age / 10 ORDER BY 1") == [
        (1, 10), (2, 20), (3, 10), (4, 10)
    ]


def test_group_by_errors(users):
    with pytest.raises(ExecutionError):
        rows(users, "SELECT name, COUNT(*) FROM users GROUP BY age")
    with pytest.raises(ExecutionError):
        rows(users, "SELECT * FROM users HAVING age > 1")
    with pytest.raises(ExecutionError):
        rows(users, "SELECT * FROM users WHERE COUNT(*) > 1")


# ----------------------------------------------------------------------------- joins
def test_inner_join(users):
    result = users.execute(
        "SELECT users.name, orders.amount FROM users INNER JOIN orders ON users.id = orders.user_id ORDER BY orders.id"
    )
    assert result.columns == ["name", "amount"]
    assert result.rows == [("Girish", 250.0), ("Girish", 99.5), ("Asha", 1200.0), ("Kiran", 40.0)]


def test_join_with_aliases_where_and_aggregate(users):
    assert rows(
        users,
        """SELECT u.name, COUNT(*) AS n, SUM(o.amount) FROM users u JOIN orders o ON o.user_id = u.id
           WHERE o.amount > 50 GROUP BY u.name ORDER BY n DESC""",
    ) == [("Girish", 2, 349.5), ("Asha", 1, 1200.0)]


def test_left_join(users):
    got = rows(users, "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON u.id = o.user_id ORDER BY u.id, o.id")
    assert got == [(1, 10), (1, 11), (2, 12), (3, None), (4, None), (5, 13)]


def test_cross_join_and_comma_join(users):
    assert rows(users, "SELECT COUNT(*) FROM users CROSS JOIN orders") == [(25,)]
    assert rows(users, "SELECT COUNT(*) FROM users, orders WHERE users.id = orders.user_id") == [(4,)]


def test_non_equi_join_uses_nested_loop(users):
    sql = "SELECT u.id, o.id FROM users u JOIN orders o ON o.amount > u.age * 20"
    assert "NestedLoopJoin" in users.explain(sql)
    assert len(rows(users, sql)) == 5  # only the 1200.0 order beats every age*20


def test_three_way_join(db):
    db.executescript(
        """
        CREATE TABLE a (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER);
        CREATE TABLE c (id INTEGER PRIMARY KEY, b_id INTEGER, v INTEGER);
        INSERT INTO a VALUES (1, 'x'), (2, 'y');
        INSERT INTO b VALUES (10, 1), (20, 2);
        INSERT INTO c VALUES (100, 10, 5), (101, 10, 6), (200, 20, 7);
        """
    )
    assert rows(db, "SELECT a.name, SUM(c.v) FROM a JOIN b ON b.a_id = a.id JOIN c ON c.b_id = b.id "
                    "GROUP BY a.name ORDER BY a.name") == [("x", 11), ("y", 7)]


def test_ambiguous_column(users):
    with pytest.raises(ColumnNotFoundError):
        rows(users, "SELECT id FROM users JOIN orders ON users.id = orders.user_id")


# ----------------------------------------------------------------------------- parameters
def test_parameters(users):
    assert rows(users, "SELECT name FROM users WHERE age >= ? AND city = ?", (18, "Bengaluru")) == [("Girish",)]
    with pytest.raises(ExecutionError):
        users.execute("SELECT * FROM users WHERE id = ?")


def test_parameters_are_not_sql_injection(users):
    evil = "x' OR '1'='1"
    assert rows(users, "SELECT * FROM users WHERE name = ?", (evil,)) == []
    users.execute("INSERT INTO users VALUES (?, ?, ?, ?)", (6, "Robert'); DROP TABLE users;--", 1, None))
    assert rows(users, "SELECT name FROM users WHERE id = 6") == [("Robert'); DROP TABLE users;--",)]
    assert "users" in users.tables()


# ----------------------------------------------------------------------------- system catalog
def test_system_tables_queryable_and_read_only(users):
    names = {r[0] for r in rows(users, "SELECT name FROM system_tables")}
    assert names == {"users", "orders"}
    assert rows(users, "SELECT COUNT(*) FROM system_columns WHERE table_id = "
                       "(1 * 0) + 0") == [(0,)]
    with pytest.raises(ExecutionError):
        users.execute("DELETE FROM system_tables")


def test_parse_errors(db):
    for bad in ["SELEC * FROM t", "SELECT * FROM", "SELECT 'unterminated", "INSERT INTO t VALUES (1",
                "CREATE TABLE t ()", "SELECT * FROM t WHERE", "SELECT 1; SELECT 2"]:
        with pytest.raises(ParseError):
            db.execute(bad)
