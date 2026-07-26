from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TypeAlias


class RoleExpressionSyntaxError(ValueError):
    """Raised when a role expression cannot be parsed."""


class RoleExpressionLimitError(RoleExpressionSyntaxError):
    """Raised when a role expression exceeds a complexity limit."""


@dataclass(frozen=True, slots=True)
class Role:
    role_id: int


@dataclass(frozen=True, slots=True)
class Not:
    operand: Expression


@dataclass(frozen=True, slots=True)
class And:
    left: Expression
    right: Expression


@dataclass(frozen=True, slots=True)
class Or:
    left: Expression
    right: Expression


Expression: TypeAlias = Role | Not | And | Or


_TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<mention><@&(?P<mention_id>[0-9]+)>)|"
    r"(?P<number>[0-9]+)|"
    r"(?P<lparen>\()|"
    r"(?P<rparen>\))|"
    r"(?P<word>[A-Za-z]+)|"
    r"(?P<invalid>\S)"
    r")"
)


def _tokenize(text: str) -> list[tuple[str, int | str | None]]:
    tokens: list[tuple[str, int | str | None]] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            if text[position:].isspace():
                break
            raise RoleExpressionSyntaxError("Invalid role expression")
        position = match.end()
        kind = match.lastgroup
        if kind == "mention":
            tokens.append(("ROLE", int(match.group("mention_id"))))
        elif kind == "number":
            tokens.append(("ROLE", int(match.group("number"))))
        elif kind == "word":
            operator = match.group("word").upper()
            if operator not in {"NOT", "AND", "OR"}:
                raise RoleExpressionSyntaxError("Unknown operator")
            tokens.append((operator, None))
        elif kind == "invalid":
            raise RoleExpressionSyntaxError("Invalid token")
        else:
            tokens.append((kind.upper(), None))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, int | str | None]]) -> None:
        self._tokens = tokens
        self._position = 0
        self._nesting_depth = 0

    def parse(self) -> Expression:
        if not self._tokens:
            raise RoleExpressionSyntaxError("Expression is empty")
        expression = self._parse_or()
        if self._peek() is not None:
            raise RoleExpressionSyntaxError("Unexpected trailing token")
        return expression

    def _parse_or(self) -> Expression:
        expression = self._parse_and()
        while self._accept("OR"):
            expression = Or(expression, self._parse_and())
        return expression

    def _parse_and(self) -> Expression:
        expression = self._parse_unary()
        while self._accept("AND"):
            expression = And(expression, self._parse_unary())
        return expression

    def _parse_unary(self) -> Expression:
        if self._accept("NOT"):
            return Not(self._parse_unary())
        return self._parse_primary()

    def _parse_primary(self) -> Expression:
        token = self._peek()
        if token is None:
            raise RoleExpressionSyntaxError("Missing operand")
        if token[0] == "ROLE":
            self._position += 1
            return Role(int(token[1]))
        if self._accept("LPAREN"):
            self._nesting_depth += 1
            if self._nesting_depth > 16:
                raise RoleExpressionLimitError(
                    "Expression nesting is deeper than 16 levels"
                )
            expression = self._parse_or()
            if not self._accept("RPAREN"):
                raise RoleExpressionSyntaxError("Unmatched parenthesis")
            self._nesting_depth -= 1
            return expression
        raise RoleExpressionSyntaxError("Expected role or parenthesized expression")

    def _peek(self) -> tuple[str, int | str | None] | None:
        if self._position >= len(self._tokens):
            return None
        return self._tokens[self._position]

    def _accept(self, kind: str) -> bool:
        token = self._peek()
        if token is None or token[0] != kind:
            return False
        self._position += 1
        return True


def parse_role_expression(text: str) -> Expression:
    if len(text) > 1_000:
        raise RoleExpressionLimitError("Expression is longer than 1,000 characters")
    tokens = _tokenize(text)
    if len(tokens) > 64:
        raise RoleExpressionLimitError("Expression contains more than 64 tokens")
    expression = _Parser(tokens).parse()
    if len(role_ids(expression)) > 20:
        raise RoleExpressionLimitError("Expression references more than 20 roles")
    return expression


def role_ids(expression: Expression) -> frozenset[int]:
    if isinstance(expression, Role):
        return frozenset((expression.role_id,))
    if isinstance(expression, Not):
        return role_ids(expression.operand)
    return role_ids(expression.left) | role_ids(expression.right)


_ROLE_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM role_analytics_memberships AS membership "
    "WHERE membership.guild_id = member.guild_id "
    "AND membership.generation = member.generation "
    "AND membership.user_id = member.user_id "
    "AND membership.role_id = ?)"
)


def compile_role_expression(expression: Expression) -> tuple[str, tuple[int, ...]]:
    if isinstance(expression, Role):
        return _ROLE_EXISTS_SQL, (expression.role_id,)
    if isinstance(expression, Not):
        sql, parameters = compile_role_expression(expression.operand)
        return f"(NOT {sql})", parameters

    left_sql, left_parameters = compile_role_expression(expression.left)
    right_sql, right_parameters = compile_role_expression(expression.right)
    operator = "AND" if isinstance(expression, And) else "OR"
    return (
        f"({left_sql} {operator} {right_sql})",
        left_parameters + right_parameters,
    )


def _precedence(expression: Expression) -> int:
    if isinstance(expression, Or):
        return 1
    if isinstance(expression, And):
        return 2
    if isinstance(expression, Not):
        return 3
    return 4


def _render(expression: Expression, parent_precedence: int) -> str:
    precedence = _precedence(expression)
    if isinstance(expression, Role):
        rendered = f"<@&{expression.role_id}>"
    elif isinstance(expression, Not):
        rendered = f"NOT {_render(expression.operand, precedence)}"
    elif isinstance(expression, And):
        rendered = (
            f"{_render(expression.left, precedence)} AND "
            f"{_render(expression.right, precedence)}"
        )
    else:
        rendered = (
            f"{_render(expression.left, precedence)} OR "
            f"{_render(expression.right, precedence)}"
        )
    if precedence < parent_precedence:
        return f"({rendered})"
    return rendered


def render_role_expression(expression: Expression) -> str:
    return _render(expression, 0)
