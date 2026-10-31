"""Recursive-descent SQL parser: tokens -> AST.

Grammar (simplified)::

    statement   := select | insert | update | delete | create_table | create_index
                 | drop_table | drop_index | BEGIN | COMMIT | ROLLBACK | EXPLAIN statement
    select      := SELECT [DISTINCT] items [FROM table_ref {join}] [WHERE expr]
                   [GROUP BY expr {, expr}] [HAVING expr]
                   [ORDER BY expr [ASC|DESC] {, ...}] [LIMIT expr [OFFSET expr]]
    join        := [INNER | LEFT | CROSS] JOIN table_ref [ON expr]

Expression precedence, lowest to highest::

    OR  <  AND  <  NOT  <  comparison (= != < <= > >= IS IN BETWEEN LIKE)
        <  + -  <  * / %  <  unary -  <  primary

Each precedence level is one method; a method parses its operands by calling
the next-higher level. This is the textbook way to build a parser by hand.
"""

from __future__ import annotations

from pydb.errors import ParseError
from pydb.sql import ast
from pydb.sql.lexer import Token, TokenType, tokenize

# Keywords that are still allowed as table/column names.
_NON_RESERVED = {"KEY", "TRANSACTION", "INDEX", "BEGIN", "COMMIT", "OFFSET", "ASC", "DESC", "IF", "LEFT"}


class Parser:
    def __init__(self, sql: str) -> None:
        self.sql = sql
        self.tokens = tokenize(sql)
        self.pos = 0
        self.param_count = 0

    # ================================================================== helpers
    @property
    def tok(self) -> Token:
        return self.tokens[self.pos]

    def peek(self, offset: int = 1) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tok
        if tok.type is not TokenType.EOF:
            self.pos += 1
        return tok

    def error(self, expected: str) -> ParseError:
        return ParseError(f"expected {expected} but found {self.tok} at position {self.tok.pos}")

    def accept_kw(self, *words: str) -> bool:
        if self.tok.is_kw(*words):
            self.advance()
            return True
        return False

    def expect_kw(self, word: str) -> None:
        if not self.accept_kw(word):
            raise self.error(word)

    def accept_punct(self, ch: str) -> bool:
        if self.tok.is_punct(ch):
            self.advance()
            return True
        return False

    def expect_punct(self, ch: str) -> None:
        if not self.accept_punct(ch):
            raise self.error(f"'{ch}'")

    def ident(self, what: str = "identifier") -> str:
        tok = self.tok
        if tok.type is TokenType.IDENT:
            self.advance()
            return tok.value
        if tok.type is TokenType.KEYWORD and tok.value in _NON_RESERVED:
            self.advance()
            return tok.value.lower()
        raise self.error(what)

    # ================================================================== entry points
    def parse_script(self) -> list[ast.Statement]:
        statements = []
        while self.tok.type is not TokenType.EOF:
            if self.accept_punct(";"):
                continue
            statements.append(self.statement())
            if not self.accept_punct(";") and self.tok.type is not TokenType.EOF:
                raise self.error("';' or end of input")
        return statements

    def statement(self) -> ast.Statement:
        t = self.tok
        if t.is_kw("SELECT"):
            return self.select()
        if t.is_kw("INSERT"):
            return self.insert()
        if t.is_kw("UPDATE"):
            return self.update()
        if t.is_kw("DELETE"):
            return self.delete()
        if t.is_kw("CREATE"):
            return self.create()
        if t.is_kw("DROP"):
            return self.drop()
        if t.is_kw("BEGIN"):
            self.advance()
            self.accept_kw("TRANSACTION")
            return ast.Begin()
        if t.is_kw("COMMIT"):
            self.advance()
            self.accept_kw("TRANSACTION")
            return ast.Commit()
        if t.is_kw("ROLLBACK"):
            self.advance()
            self.accept_kw("TRANSACTION")
            return ast.Rollback()
        if t.is_kw("EXPLAIN"):
            self.advance()
            inner = self.statement()
            if not isinstance(inner, (ast.Select, ast.Update, ast.Delete)):
                raise ParseError("EXPLAIN supports SELECT, UPDATE and DELETE")
            return ast.Explain(inner)
        raise self.error("a SQL statement")

    # ================================================================== DDL
    def create(self) -> ast.Statement:
        self.expect_kw("CREATE")
        unique = self.accept_kw("UNIQUE")
        if self.accept_kw("INDEX"):
            if_not_exists = self._if_not_exists()
            name = self.ident("index name")
            self.expect_kw("ON")
            table = self.ident("table name")
            self.expect_punct("(")
            column = self.ident("column name")
            if self.tok.is_punct(","):
                raise ParseError("multi-column indexes are not supported")
            self.expect_punct(")")
            return ast.CreateIndex(name, table, column, unique, if_not_exists)
        if unique:
            raise self.error("INDEX")
        self.expect_kw("TABLE")
        if_not_exists = self._if_not_exists()
        name = self.ident("table name")
        self.expect_punct("(")
        columns: list[ast.ColumnDef] = []
        table_pk: list[str] = []
        while True:
            if self.tok.is_kw("PRIMARY"):
                self.advance()
                self.expect_kw("KEY")
                self.expect_punct("(")
                table_pk.append(self.ident("column name"))
                if self.tok.is_punct(","):
                    raise ParseError("composite primary keys are not supported")
                self.expect_punct(")")
            else:
                columns.append(self.column_def())
            if not self.accept_punct(","):
                break
        self.expect_punct(")")
        for pk in table_pk:
            match = [c for c in columns if c.name == pk]
            if not match:
                raise ParseError(f"PRIMARY KEY column {pk!r} is not defined")
            match[0].primary_key = True
        return ast.CreateTable(name, columns, if_not_exists)

    def _if_not_exists(self) -> bool:
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            return True
        return False

    def column_def(self) -> ast.ColumnDef:
        name = self.ident("column name")
        tok = self.tok
        if tok.type not in (TokenType.IDENT, TokenType.KEYWORD):
            raise self.error("column type")
        self.advance()
        col = ast.ColumnDef(name, tok.value.upper())
        if self.accept_punct("("):  # VARCHAR(255) — length accepted and ignored
            while not self.accept_punct(")"):
                if self.tok.type is TokenType.EOF:
                    raise self.error("')'")
                self.advance()
        while True:
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                col.primary_key = True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                col.not_null = True
            elif self.accept_kw("NULL"):
                pass
            elif self.accept_kw("UNIQUE"):
                col.unique = True
            elif self.accept_kw("DEFAULT"):
                col.default = self.unary()
            elif self.accept_kw("REFERENCES"):
                table = self.ident("referenced table")
                self.expect_punct("(")
                column = self.ident("referenced column")
                self.expect_punct(")")
                col.references = (table, column)
            else:
                return col

    def drop(self) -> ast.Statement:
        self.expect_kw("DROP")
        if self.accept_kw("TABLE"):
            if_exists = self._if_exists()
            return ast.DropTable(self.ident("table name"), if_exists)
        if self.accept_kw("INDEX"):
            if_exists = self._if_exists()
            return ast.DropIndex(self.ident("index name"), if_exists)
        raise self.error("TABLE or INDEX")

    def _if_exists(self) -> bool:
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            return True
        return False

    # ================================================================== DML
    def insert(self) -> ast.Insert:
        self.expect_kw("INSERT")
        self.expect_kw("INTO")
        table = self.ident("table name")
        columns = None
        if self.accept_punct("("):
            columns = [self.ident("column name")]
            while self.accept_punct(","):
                columns.append(self.ident("column name"))
            self.expect_punct(")")
        self.expect_kw("VALUES")
        rows = [self._value_row()]
        while self.accept_punct(","):
            rows.append(self._value_row())
        return ast.Insert(table, columns, rows)

    def _value_row(self) -> list[ast.Expr]:
        self.expect_punct("(")
        values = [self.expr()]
        while self.accept_punct(","):
            values.append(self.expr())
        self.expect_punct(")")
        return values

    def update(self) -> ast.Update:
        self.expect_kw("UPDATE")
        table = self.ident("table name")
        self.expect_kw("SET")
        assignments = []
        while True:
            col = self.ident("column name")
            if not self.tok.is_op("="):
                raise self.error("'='")
            self.advance()
            assignments.append((col, self.expr()))
            if not self.accept_punct(","):
                break
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Update(table, assignments, where)

    def delete(self) -> ast.Delete:
        self.expect_kw("DELETE")
        self.expect_kw("FROM")
        table = self.ident("table name")
        where = self.expr() if self.accept_kw("WHERE") else None
        return ast.Delete(table, where)

    # ================================================================== SELECT
    def select(self) -> ast.Select:
        self.expect_kw("SELECT")
        distinct = self.accept_kw("DISTINCT")
        items = [self.select_item()]
        while self.accept_punct(","):
            items.append(self.select_item())
        stmt = ast.Select(items, None, distinct=distinct)
        if self.accept_kw("FROM"):
            stmt.from_table = self.table_ref()
            while True:
                if self.accept_punct(","):  # FROM a, b  == CROSS JOIN
                    stmt.joins.append(ast.Join(self.table_ref(), None, "CROSS"))
                    continue
                kind = None
                if self.tok.is_kw("JOIN"):
                    kind = "INNER"
                elif self.tok.is_kw("INNER"):
                    self.advance()
                    kind = "INNER"
                elif self.tok.is_kw("LEFT"):
                    self.advance()
                    kind = "LEFT"
                elif self.tok.is_kw("CROSS"):
                    self.advance()
                    kind = "CROSS"
                if kind is None:
                    break
                self.expect_kw("JOIN")
                ref = self.table_ref()
                cond = None
                if kind != "CROSS":
                    self.expect_kw("ON")
                    cond = self.expr()
                stmt.joins.append(ast.Join(ref, cond, kind))
        if self.accept_kw("WHERE"):
            stmt.where = self.expr()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            stmt.group_by = [self.expr()]
            while self.accept_punct(","):
                stmt.group_by.append(self.expr())
        if self.accept_kw("HAVING"):
            stmt.having = self.expr()
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            stmt.order_by = [self.order_item()]
            while self.accept_punct(","):
                stmt.order_by.append(self.order_item())
        if self.accept_kw("LIMIT"):
            stmt.limit = self.expr()
            if self.accept_kw("OFFSET"):
                stmt.offset = self.expr()
        return stmt

    def select_item(self) -> ast.SelectItem:
        if self.tok.is_op("*"):
            self.advance()
            return ast.SelectItem(ast.Star())
        if self.tok.type is TokenType.IDENT and self.peek().is_punct(".") and self.peek(2).is_op("*"):
            table = self.advance().value
            self.advance()
            self.advance()
            return ast.SelectItem(ast.Star(table))
        expr = self.expr()
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident("alias")
        elif self.tok.type is TokenType.IDENT:
            alias = self.advance().value
        return ast.SelectItem(expr, alias)

    def table_ref(self) -> ast.TableRef:
        name = self.ident("table name")
        alias = None
        if self.accept_kw("AS"):
            alias = self.ident("alias")
        elif self.tok.type is TokenType.IDENT:
            alias = self.advance().value
        return ast.TableRef(name, alias)

    def order_item(self) -> ast.OrderItem:
        expr = self.expr()
        if self.accept_kw("DESC"):
            return ast.OrderItem(expr, True)
        self.accept_kw("ASC")
        return ast.OrderItem(expr, False)

    # ================================================================== expressions
    def expr(self) -> ast.Expr:
        return self.or_expr()

    def or_expr(self) -> ast.Expr:
        left = self.and_expr()
        while self.accept_kw("OR"):
            left = ast.BinaryOp("OR", left, self.and_expr())
        return left

    def and_expr(self) -> ast.Expr:
        left = self.not_expr()
        while self.accept_kw("AND"):
            left = ast.BinaryOp("AND", left, self.not_expr())
        return left

    def not_expr(self) -> ast.Expr:
        if self.accept_kw("NOT"):
            return ast.UnaryOp("NOT", self.not_expr())
        return self.comparison()

    def comparison(self) -> ast.Expr:
        left = self.additive()
        while True:
            if self.tok.is_op("=", "!=", "<", "<=", ">", ">="):
                op = self.advance().value
                left = ast.BinaryOp(op, left, self.additive())
            elif self.tok.is_kw("IS"):
                self.advance()
                negated = self.accept_kw("NOT")
                self.expect_kw("NULL")
                left = ast.IsNull(left, negated)
            elif self.tok.is_kw("NOT") and self.peek().is_kw("IN", "BETWEEN", "LIKE"):
                self.advance()
                left = self._postfix_predicate(left, negated=True)
            elif self.tok.is_kw("IN", "BETWEEN", "LIKE"):
                left = self._postfix_predicate(left, negated=False)
            else:
                return left

    def _postfix_predicate(self, left: ast.Expr, negated: bool) -> ast.Expr:
        if self.accept_kw("IN"):
            self.expect_punct("(")
            items = [self.expr()]
            while self.accept_punct(","):
                items.append(self.expr())
            self.expect_punct(")")
            return ast.InList(left, tuple(items), negated)
        if self.accept_kw("BETWEEN"):
            low = self.additive()
            self.expect_kw("AND")
            high = self.additive()
            return ast.Between(left, low, high, negated)
        self.expect_kw("LIKE")
        node: ast.Expr = ast.BinaryOp("LIKE", left, self.additive())
        return ast.UnaryOp("NOT", node) if negated else node

    def additive(self) -> ast.Expr:
        left = self.multiplicative()
        while self.tok.is_op("+", "-"):
            op = self.advance().value
            left = ast.BinaryOp(op, left, self.multiplicative())
        return left

    def multiplicative(self) -> ast.Expr:
        left = self.unary()
        while self.tok.is_op("*", "/", "%"):
            op = self.advance().value
            left = ast.BinaryOp(op, left, self.unary())
        return left

    def unary(self) -> ast.Expr:
        if self.tok.is_op("-", "+"):
            op = self.advance().value
            operand = self.unary()
            if isinstance(operand, ast.Literal) and isinstance(operand.value, (int, float)) \
                    and not isinstance(operand.value, bool):
                return ast.Literal(-operand.value if op == "-" else operand.value)  # fold constants
            return ast.UnaryOp(op, operand)
        return self.primary()

    def primary(self) -> ast.Expr:
        tok = self.tok
        if tok.type is TokenType.INTEGER:
            self.advance()
            return ast.Literal(int(tok.value))
        if tok.type is TokenType.FLOAT:
            self.advance()
            return ast.Literal(float(tok.value))
        if tok.type is TokenType.STRING:
            self.advance()
            return ast.Literal(tok.value)
        if tok.type is TokenType.PARAM:
            self.advance()
            self.param_count += 1
            return ast.Param(self.param_count - 1)
        if tok.is_kw("NULL"):
            self.advance()
            return ast.Literal(None)
        if tok.is_kw("TRUE", "FALSE"):
            self.advance()
            return ast.Literal(tok.value == "TRUE")
        if self.accept_punct("("):
            inner = self.expr()
            self.expect_punct(")")
            return inner
        if tok.type is TokenType.IDENT or (tok.type is TokenType.KEYWORD and tok.value in _NON_RESERVED):
            name = self.ident()
            if self.tok.is_punct("("):
                return self.function_call(name.upper())
            if self.accept_punct("."):
                return ast.ColumnRef(self.ident("column name"), name)
            return ast.ColumnRef(name)
        raise self.error("an expression")

    def function_call(self, name: str) -> ast.FuncCall:
        self.expect_punct("(")
        if self.tok.is_op("*"):
            self.advance()
            self.expect_punct(")")
            if name != "COUNT":
                raise ParseError(f"{name}(*) is not valid; only COUNT(*)")
            return ast.FuncCall(name, (), star=True)
        distinct = self.accept_kw("DISTINCT")
        args: list[ast.Expr] = []
        if not self.tok.is_punct(")"):
            args.append(self.expr())
            while self.accept_punct(","):
                args.append(self.expr())
        self.expect_punct(")")
        return ast.FuncCall(name, tuple(args), distinct=distinct)


def parse(sql: str) -> list[ast.Statement]:
    """Parse a script of one or more ``;``-separated statements."""
    return Parser(sql).parse_script()


def parse_one(sql: str) -> tuple[ast.Statement, int]:
    """Parse exactly one statement; returns it with its number of ``?`` parameters."""
    parser = Parser(sql)
    statements = parser.parse_script()
    if len(statements) != 1:
        raise ParseError(f"expected exactly one statement, got {len(statements)}")
    return statements[0], parser.param_count
