"""Integration tests: persistence, catalog, planner choices, indexes through SQL, randomized model checks."""

from __future__ import annotations

import random

import pytest

from pydb import Database
from pydb.errors import (
    ConstraintViolationError,
    DatabaseError,
    DatabaseLockedError,
    ExecutionError,
    IndexNotFoundError,
)


# ----------------------------------------------------------------------------- Phase 1 API
def test_phase1_api(db_path):
    db = Database(db_path)
    db.create_table("users", {"id": "INTEGER", "name": "TEXT", "age": "INTEGER"})
    db.insert("users", {"id": 1, "name": "Girish", "age": 21})
    db.insert("users", {"id": 2, "name": "Asha", "age": 30})
    assert db.select("users") == [{"id": 1, "name": "Girish", "age": 21}, {"id": 2, "name": "Asha", "age": 30}]
    assert db.select("users", where={"name": "Asha"}, columns=["age"]) == [{"age": 30}]
    assert db.update("users", {"age": 22}, where={"id": 1}) == 1
    assert db.delete("users", where={"id": 2}) == 1
    db.close()
    db = Database(db_path)
    assert db.select("users") == [{"id": 1, "name": "Girish", "age": 22}]
    db.drop_table("users")
    assert db.tables() == []
    db.close()


def test_context_manager_and_closed_state(db_path):
    with Database(db_path) as db:
        db.execute("CREATE TABLE t (a INTEGER)")
    assert db.closed
    with pytest.raises(DatabaseError):
        db.execute("SELECT * FROM t")


def test_file_lock_prevents_second_open(db_path):
    db = Database(db_path)
    with pytest.raises(DatabaseLockedError):
        Database(db_path)
    db.close()


# ----------------------------------------------------------------------------- persistence
def test_catalog_and_data_persist(db_path):
    db = Database(db_path)
    db.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, age INTEGER DEFAULT 18);
        CREATE TABLE posts (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), title TEXT);
        CREATE INDEX idx_users_age ON users(age);
        INSERT INTO users (id, email) VALUES (1, 'a@x');
        INSERT INTO posts VALUES (10, 1, 'hello');
        """
    )
    schema_before = db.schema("users")
    db.close()

    db = Database(db_path)
    assert db.tables() == ["posts", "users"]
    assert db.schema("users") == schema_before
    assert "DEFAULT 18" in schema_before and "UNIQUE" in schema_before
    assert {i["name"] for i in db.indexes()} == {"pk_users", "uq_users_email", "idx_users_age", "pk_posts"}
    assert db.execute("SELECT * FROM users").rows == [(1, "a@x", 18)]
    with pytest.raises(ConstraintViolationError):  # constraints still enforced after reopen
        db.execute("INSERT INTO posts VALUES (11, 99, 'orphan')")
    db.close()


def test_large_table_persists_and_spans_many_pages(db_path):
    db = Database(db_path, buffer_pool_size=16)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, payload TEXT)")
    with db.transaction():
        for i in range(3000):
            db.execute("INSERT INTO t VALUES (?, ?)", (i, f"payload-{i:05d}-" + "z" * 40))
    db.close()
    db = Database(db_path, buffer_pool_size=16)
    assert db.stats()["pages"] > 50
    assert db.execute("SELECT COUNT(*) FROM t").scalar() == 3000
    assert db.execute("SELECT payload FROM t WHERE id = 2999").scalar().startswith("payload-02999")
    assert db.stats()["buffer_pool"]["evictions"] > 0
    db.close()


# ----------------------------------------------------------------------------- planner
@pytest.fixture
def people(db):
    db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, age INTEGER, name TEXT)")
    with db.transaction():
        for i in range(1, 501):
            db.execute("INSERT INTO people VALUES (?, ?, ?)", (i, i % 60, f"p{i}"))
    return db


def test_planner_uses_primary_key_index(people):
    plan = people.explain("SELECT * FROM people WHERE id = 250")
    assert "IndexScan(people, pk_people: id = 250)" in plan
    assert people.execute("SELECT name FROM people WHERE id = 250").rows == [("p250",)]


def test_planner_seq_scan_without_index(people):
    plan = people.explain("SELECT * FROM people WHERE age = 7")
    assert "SeqScan(people)" in plan and "Filter(age = 7)" in plan


def test_create_index_changes_plan_and_results_match(people):
    sql = "SELECT id FROM people WHERE age = 7 ORDER BY id"
    before = people.execute(sql).rows
    people.execute("CREATE INDEX idx_people_age ON people(age)")
    assert "IndexScan(people, idx_people_age: age = 7)" in people.explain(sql)
    assert people.execute(sql).rows == before
    people.execute("DROP INDEX idx_people_age")
    assert "SeqScan" in people.explain(sql)
    with pytest.raises(IndexNotFoundError):
        people.execute("DROP INDEX idx_people_age")
    with pytest.raises(ExecutionError):
        people.execute("DROP INDEX pk_people")  # backs the PRIMARY KEY


def test_range_scans_merge_bounds(people):
    people.execute("CREATE INDEX idx_people_age ON people(age)")
    sql = "SELECT COUNT(*) FROM people WHERE age >= 10 AND age < 20 AND name LIKE 'p1%'"
    plan = people.explain(sql)
    assert "idx_people_age: age >= 10 AND age < 20" in plan
    assert "Filter(name LIKE 'p1%')" in plan
    expected = len([i for i in range(1, 501) if 10 <= i % 60 < 20 and str(i).startswith("1")])
    assert people.execute(sql).scalar() == expected
    assert "age >= 5 AND age <= 6" in people.explain("SELECT * FROM people WHERE age BETWEEN 5 AND 6")
    assert "age > 58" in people.explain("SELECT * FROM people WHERE 58 < age")


def test_equality_preferred_over_range(people):
    people.execute("CREATE INDEX idx_people_age ON people(age)")
    plan = people.explain("SELECT * FROM people WHERE age > 3 AND id = 10")
    assert "pk_people: id = 10" in plan


def test_index_not_used_for_incompatible_constant(people):
    with pytest.raises(ExecutionError):
        people.execute("SELECT * FROM people WHERE id = 'ten'")


def test_join_algorithm_selection(people):
    people.execute("CREATE TABLE pets (pid INTEGER PRIMARY KEY, owner INTEGER, kind TEXT)")
    people.execute("INSERT INTO pets VALUES (1, 10, 'cat'), (2, 10, 'dog'), (3, 20, 'fish')")
    # inner side people has an index on id -> index nested loop join
    plan = people.explain("SELECT * FROM pets JOIN people ON people.id = pets.owner")
    assert "IndexNestedLoopJoin" in plan
    # no index on pets.owner -> hash join
    plan = people.explain("SELECT * FROM people JOIN pets ON pets.owner = people.id")
    assert "HashJoin" in plan
    a = sorted(people.execute("SELECT people.name, pets.kind FROM pets JOIN people ON people.id = pets.owner"))
    b = sorted(people.execute("SELECT people.name, pets.kind FROM people JOIN pets ON pets.owner = people.id"))
    assert a == b == [("p10", "cat"), ("p10", "dog"), ("p20", "fish")]


def test_predicate_pushdown_in_explain(people):
    people.execute("CREATE TABLE pets (pid INTEGER PRIMARY KEY, owner INTEGER, kind TEXT)")
    plan = people.explain(
        "SELECT * FROM people JOIN pets ON pets.owner = people.id WHERE pets.kind = 'cat' AND people.age > 5"
    )
    lines = plan.splitlines()
    join_line = next(i for i, ln in enumerate(lines) if "Join" in ln)
    assert any("Filter(pets.kind = 'cat')" in ln for ln in lines[join_line:])  # below the join
    assert any("Filter(people.age > 5)" in ln for ln in lines[join_line:])


def test_explain_update_and_delete(people):
    assert "IndexScan(people, pk_people: id = 3)" in people.explain("UPDATE people SET age = 1 WHERE id = 3")
    assert people.explain("DELETE FROM people WHERE age = 1").startswith("Delete(people)")


def test_buffer_pool_statistics_exposed(people):
    people.execute("SELECT * FROM people")
    people.execute("SELECT * FROM people")
    stats = people.stats()["buffer_pool"]
    assert stats["hits"] > 0 and 0 < stats["hit_ratio"] <= 1


# ----------------------------------------------------------------------------- randomized model test
def test_randomized_sql_against_python_model(db_path):
    rng = random.Random(99)
    db = Database(db_path, buffer_pool_size=12, btree_max_entries=8)
    db.execute("CREATE TABLE m (id INTEGER PRIMARY KEY, grp INTEGER, label TEXT)")
    db.execute("CREATE INDEX m_grp ON m(grp)")
    model: dict[int, tuple[int, str]] = {}
    for step in range(1500):
        op = rng.random()
        if op < 0.45:
            k = rng.randint(0, 500)
            g, lbl = rng.randint(0, 20), "L" * rng.randint(0, 60)
            if k in model:
                with pytest.raises(ConstraintViolationError):
                    db.execute("INSERT INTO m VALUES (?, ?, ?)", (k, g, lbl))
            else:
                db.execute("INSERT INTO m VALUES (?, ?, ?)", (k, g, lbl))
                model[k] = (g, lbl)
        elif op < 0.65:
            g = rng.randint(0, 20)
            new = rng.randint(0, 20)
            n = db.execute("UPDATE m SET grp = ?, label = label WHERE grp = ?", (new, g)).rowcount
            hits = [k for k, (gg, _) in model.items() if gg == g]
            assert n == len(hits)
            for k in hits:
                model[k] = (new, model[k][1])
        elif op < 0.8:
            lo = rng.randint(0, 500)
            n = db.execute("DELETE FROM m WHERE id >= ? AND id < ?", (lo, lo + 15)).rowcount
            gone = [k for k in model if lo <= k < lo + 15]
            assert n == len(gone)
            for k in gone:
                del model[k]
        else:
            g = rng.randint(0, 20)
            got = sorted(db.execute("SELECT id FROM m WHERE grp = ?", (g,)))
            assert got == sorted((k,) for k, (gg, _) in model.items() if gg == g)
        if step % 300 == 0:
            db.close()
            db = Database(db_path, buffer_pool_size=12, btree_max_entries=8)
    assert sorted(db.execute("SELECT id, grp, label FROM m")) == sorted((k, g, lbl) for k, (g, lbl) in model.items())
    agg = dict(db.execute("SELECT grp, COUNT(*) FROM m GROUP BY grp").rows)
    expected: dict[int, int] = {}
    for g, _ in model.values():
        expected[g] = expected.get(g, 0) + 1
    assert agg == expected
    for info in db.engine.catalog.indexes.values():
        db.engine.indexes.tree(info).check_invariants()
    db.close()
