# Query engine

Source: `pydb/sql/` (language) and `pydb/execution/` (planning and execution).

```text
SQL text -> Lexer -> tokens -> Parser -> AST -> Planner -> operator tree -> rows
```

## Lexer (`sql/lexer.py`)

A hand-written scanner that walks the text character by character. Token types:
KEYWORD, IDENT, INTEGER, FLOAT, STRING, PARAM (`?`), OP, PUNCT, EOF.

* Strings use single quotes, `''` escapes a quote: `'it''s'`.
* `"quoted identifiers"` allow keywords as names.
* Comments: `-- line` and `/* block */`.
* Identifiers are case-insensitive (lower-cased); `<>` is normalized to `!=`.

## Parser (`sql/parser.py`)

Recursive descent: one method per grammar rule and per precedence level.

```text
OR  <  AND  <  NOT  <  comparison (= != < <= > >=, IS [NOT] NULL, [NOT] IN, [NOT] BETWEEN, [NOT] LIKE)
    <  + -  <  * / %  <  unary - +  <  primary (literal, ?, column, table.column, function(...), (expr))
```

Statements: `CREATE TABLE [IF NOT EXISTS]`, `DROP TABLE [IF EXISTS]`,
`CREATE [UNIQUE] INDEX [IF NOT EXISTS] name ON t(col)`, `DROP INDEX [IF EXISTS]`,
`INSERT INTO t [(cols)] VALUES (...), (...)`, `SELECT`, `UPDATE`, `DELETE`,
`BEGIN`, `COMMIT`, `ROLLBACK`, `EXPLAIN <select|update|delete>`.

SELECT supports `DISTINCT`, `*` / `t.*`, aliases, `FROM a [AS x]`,
`[INNER] JOIN`, `LEFT JOIN`, `CROSS JOIN`, `FROM a, b`, `WHERE`, `GROUP BY`, `HAVING`,
`ORDER BY expr|alias|position [ASC|DESC]`, `LIMIT n [OFFSET m]`.

Column constraints: `PRIMARY KEY`, `NOT NULL`, `UNIQUE`, `DEFAULT <literal>`,
`REFERENCES parent(col)`; table constraint `PRIMARY KEY (col)`.
Types: `INTEGER` (INT, BIGINT), `FLOAT` (REAL, DOUBLE), `TEXT` (VARCHAR(n), CHAR), `BOOLEAN` (BOOL).

## AST (`sql/ast.py`)

Frozen dataclasses (`Literal`, `Param`, `ColumnRef`, `BinaryOp`, `FuncCall`, …) and
statement classes. Structural equality lets the planner match `GROUP BY age` with `age`
in the SELECT list. `expr_to_sql` renders expressions back for headers and EXPLAIN.

## Expressions (`sql/expressions.py`)

Expressions are **compiled** into Python closures once per query. Column references are
resolved to tuple positions during compilation ("binding"), so evaluating a row is a plain
function call — no name lookups and no `eval()`/`exec()` anywhere.

* Three-valued logic: comparisons with NULL give NULL; `NULL AND FALSE = FALSE`,
  `NULL OR TRUE = TRUE`, `NOT NULL = NULL`; WHERE keeps a row only if the result is TRUE.
* Integer division truncates toward zero; division by zero yields NULL (as in SQLite).
* Comparing incompatible types (TEXT vs INTEGER) is an error rather than a silent guess.
* `LIKE` (`%`, `_`, case-insensitive), `IN (...)`, `BETWEEN`, scalar functions
  `UPPER LOWER LENGTH ABS ROUND COALESCE`.

## Parameters

`?` placeholders become `Param(i)` nodes in the AST. Values are bound when the expression
is compiled. Because parameters never become SQL text, `"x' OR '1'='1"` is just a string
value — SQL injection is impossible through parameters (there is a test for this).

## Operators (`execution/*.py`) — the iterator model

Every operator has `columns` (its output schema) and is iterable; parents pull rows from
children. `LIMIT 10` therefore stops a scan after ten rows.

| Operator | What it does |
|---|---|
| `SeqScan` | every row of a heap file, page by page |
| `IndexScan` | B+ tree point or range lookup, then fetch rows by RID |
| `SystemTableScan` | rows of `system_tables` / `system_columns` / `system_indexes` |
| `Filter` | keep rows where the predicate is TRUE |
| `Projection` | compute the SELECT list |
| `NestedLoopJoin` | any join condition; inner side materialized once |
| `HashJoin` | equi-join; build a hash table on the inner input, probe with the outer |
| `IndexNestedLoopJoin` | equi-join; probe the inner table's B+ tree per outer row |
| `HashAggregate` | GROUP BY + COUNT/SUM/AVG/MIN/MAX (with DISTINCT) |
| `Sort` | ORDER BY (stable multi-key sort, NULLs first) |
| `Distinct`, `Limit` | DISTINCT, LIMIT/OFFSET |
| `SingleRow` | input for `SELECT 1 + 1` with no FROM |

All joins support INNER and LEFT (unmatched outer rows padded with NULLs).

## Planner (`execution/planner.py`)

A rule-based optimizer. Steps for a SELECT:

1. **Bind sources** — each table in FROM/JOIN with its alias; duplicate names are rejected.
2. **Predicate pushdown** — WHERE is split into AND-conjuncts. A conjunct that references
   exactly one table is attached to that table's scan (not for the right side of a LEFT
   JOIN, where that would change the result). The rest is applied after the joins.
3. **Access path selection** (per table) — find sargable conjuncts on indexed columns
   whose constant is type-compatible. Ranking: equality > range, unique > non-unique.
   Range conjuncts on one column are merged (`age >= 10 AND age < 20` → one scan). The
   conjuncts consumed by the index are removed; the rest become a `Filter`.
4. **Join algorithm selection** (left-deep, in the written order) — if the ON clause
   contains `outer_expr = inner.col`: `IndexNestedLoopJoin` when `inner.col` is indexed,
   else `HashJoin`; other conjuncts become a residual condition. No equality → `NestedLoopJoin`.
5. **Aggregation** — if there is GROUP BY or any aggregate, a `HashAggregate` is added and
   the SELECT/HAVING/ORDER BY expressions are rewritten to refer to its output slots. A
   bare column that is not grouped is an error.
6. `HAVING` filter, `Sort`, `Projection`, `Distinct`, `Limit`.

Example:

```text
pydb> EXPLAIN SELECT people.name, COUNT(*) FROM people JOIN pets ON pets.owner = people.id
....> WHERE pets.kind = 'cat' AND people.age > 5 GROUP BY people.name ORDER BY 2 DESC LIMIT 3;
Limit(3)
-> Projection(name, COUNT(*))
  -> Sort(2 DESC)
    -> HashAggregate(group by people.name; COUNT(*))
      -> HashJoin(INNER ON pets.owner = people.id)
        -> Filter(people.age > 5)
          -> SeqScan(people)  rows~0
        -> Filter(pets.kind = 'cat')
          -> SeqScan(pets)  rows~0
```

`UPDATE` and `DELETE` use the same access-path selection. Target rows are collected
before any are modified, so an update that moves a row cannot visit it twice (the
"Halloween problem").

### What the planner does not do

No cost model or histograms (only row counts, shown as `rows~N`), no join reordering,
no subqueries, no index-only scans, no sort avoidance via indexes. Sorts, hash tables
and aggregates are in memory.
