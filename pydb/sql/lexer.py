"""SQL lexer: turns SQL text into a list of tokens.

A hand-written scanner (no regex-based splitting): it walks the input one
character at a time, which makes error positions exact and handles quoting
correctly — e.g. ``'it''s'`` is one string token containing an apostrophe.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from pydb.errors import ParseError


class TokenType(Enum):
    KEYWORD = auto()
    IDENT = auto()
    INTEGER = auto()
    FLOAT = auto()
    STRING = auto()
    PARAM = auto()  # ?
    OP = auto()  # = != <> < > <= >= + - * / %
    PUNCT = auto()  # ( ) , ; .
    EOF = auto()


KEYWORDS = frozenset(
    """
    SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE DROP TABLE INDEX ON
    AND OR NOT NULL IS TRUE FALSE PRIMARY KEY UNIQUE DEFAULT REFERENCES FOREIGN
    INNER JOIN LEFT CROSS AS ORDER BY ASC DESC LIMIT OFFSET GROUP HAVING DISTINCT
    BEGIN COMMIT ROLLBACK TRANSACTION EXPLAIN IF EXISTS IN BETWEEN LIKE
    """.split()
)


@dataclass(frozen=True)
class Token:
    type: TokenType
    value: str
    pos: int

    def is_kw(self, *words: str) -> bool:
        return self.type is TokenType.KEYWORD and self.value in words

    def is_punct(self, ch: str) -> bool:
        return self.type is TokenType.PUNCT and self.value == ch

    def is_op(self, *ops: str) -> bool:
        return self.type is TokenType.OP and self.value in ops

    def __str__(self) -> str:
        return "end of input" if self.type is TokenType.EOF else repr(self.value)


_TWO_CHAR_OPS = {"!=", "<>", "<=", ">=", "=="}
_ONE_CHAR_OPS = set("=<>+-*/%")
_PUNCT = set("(),;.")


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
        elif sql.startswith("--", i):  # line comment
            while i < n and sql[i] != "\n":
                i += 1
        elif sql.startswith("/*", i):  # block comment
            end = sql.find("*/", i + 2)
            if end < 0:
                raise ParseError(f"unterminated comment starting at position {i}")
            i = end + 2
        elif ch == "'":
            i = _string(sql, i, tokens)
        elif ch == '"':  # quoted identifier
            end = sql.find('"', i + 1)
            if end < 0:
                raise ParseError(f"unterminated quoted identifier at position {i}")
            tokens.append(Token(TokenType.IDENT, sql[i + 1 : end].lower(), i))
            i = end + 1
        elif ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            i = _number(sql, i, tokens)
        elif ch.isalpha() or ch == "_":
            start = i
            while i < n and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token(TokenType.KEYWORD, upper, start))
            else:
                tokens.append(Token(TokenType.IDENT, word.lower(), start))
        elif ch == "?":
            tokens.append(Token(TokenType.PARAM, "?", i))
            i += 1
        elif sql[i : i + 2] in _TWO_CHAR_OPS:
            op = sql[i : i + 2]
            tokens.append(Token(TokenType.OP, {"<>": "!=", "==": "="}.get(op, op), i))
            i += 2
        elif ch in _ONE_CHAR_OPS:
            tokens.append(Token(TokenType.OP, ch, i))
            i += 1
        elif ch in _PUNCT:
            tokens.append(Token(TokenType.PUNCT, ch, i))
            i += 1
        else:
            raise ParseError(f"unexpected character {ch!r} at position {i}")
    tokens.append(Token(TokenType.EOF, "", n))
    return tokens


def _string(sql: str, i: int, tokens: list[Token]) -> int:
    start, i = i, i + 1
    parts: list[str] = []
    while True:
        end = sql.find("'", i)
        if end < 0:
            raise ParseError(f"unterminated string literal starting at position {start}")
        parts.append(sql[i:end])
        if end + 1 < len(sql) and sql[end + 1] == "'":  # '' is an escaped quote
            parts.append("'")
            i = end + 2
            continue
        tokens.append(Token(TokenType.STRING, "".join(parts), start))
        return end + 1


def _number(sql: str, i: int, tokens: list[Token]) -> int:
    start, n = i, len(sql)
    seen_dot = seen_exp = False
    while i < n:
        ch = sql[i]
        if ch.isdigit():
            i += 1
        elif ch == "." and not seen_dot and not seen_exp:
            seen_dot = True
            i += 1
        elif ch in "eE" and not seen_exp and i + 1 < n and (sql[i + 1].isdigit() or sql[i + 1] in "+-"):
            seen_exp = True
            i += 2
        else:
            break
    text = sql[start:i]
    if i < n and (sql[i].isalpha() or sql[i] == "_"):
        raise ParseError(f"invalid number {sql[start:i + 1]!r} at position {start}")
    tokens.append(Token(TokenType.FLOAT if (seen_dot or seen_exp) else TokenType.INTEGER, text, start))
    return i
