"""Fail-closed, token-aware SQL facts for optimizer-facing projections.

This is deliberately not a SQL validator or semantic equivalence engine.  It
collects only direct PostgreSQL Query-slice references and permits sample-row
downgrading for a substantially smaller, syntactically flat subset.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?")
_DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_0-9]*\$")
_CLAUSE_KEYWORDS = frozenset(
    {
        "DISTINCT",
        "EXCEPT",
        "FROM",
        "GROUP",
        "HAVING",
        "INTERSECT",
        "JOIN",
        "LIMIT",
        "LATERAL",
        "OFFSET",
        "ORDER",
        "SELECT",
        "UNION",
        "WHERE",
        "WINDOW",
    }
)
_ALIAS_STOP_WORDS = _CLAUSE_KEYWORDS | frozenset(
    {"AS", "CROSS", "FULL", "INNER", "LEFT", "NATURAL", "ON", "OUTER", "RIGHT"}
)
_AGGREGATES = frozenset({"COUNT", "MIN", "MAX", "SUM", "AVG"})
_SAMPLE_UNSUPPORTED = frozenset(
    {
        "EXCEPT",
        "GROUP",
        "HAVING",
        "INTERSECT",
        "OVER",
        "UNION",
        "WITH",
        "WINDOW",
    }
)


@dataclass(frozen=True)
class SqlReferences:
    """Direct references and query shape, or an explicit unparsed result."""

    parse_status: str
    tables: tuple[str, ...] = ()
    qualified_columns: tuple[str, ...] = ()
    clauses: tuple[str, ...] = ()
    has_cte: bool = False
    has_subquery: bool = False
    has_window: bool = False
    has_set_operation: bool = False
    flat_scope: str | None = None
    sample_fields: tuple[str, ...] = ()
    aggregate_fields: tuple[str, ...] = ()
    is_limited_sample: bool = False


def extract_sql_references(query: str) -> SqlReferences:
    """Extract direct Query-slice references without interpreting SQL semantics."""

    try:
        tokens = _tokenize(query)
    except ValueError:
        return SqlReferences(parse_status="sql_unparsed")
    if not tokens or not any(token == "SELECT" for token in tokens):
        return SqlReferences(parse_status="sql_unparsed")

    cte_names = _cte_names(tokens)
    tables, aliases, has_subquery = _tables_and_aliases(tokens, cte_names)
    has_subquery = has_subquery or any(
        tokens[index : index + 2] == ("(", "SELECT") for index in range(len(tokens) - 1)
    )
    columns = _qualified_columns(tokens, aliases, cte_names)
    clauses = tuple(sorted(token for token in set(tokens) if token in _CLAUSE_KEYWORDS))
    has_cte = "WITH" in tokens
    has_window = "OVER" in tokens
    has_set_operation = any(token in {"UNION", "INTERSECT", "EXCEPT"} for token in tokens)
    flat_scope = _flat_scope(tokens, has_subquery, has_cte, has_window, has_set_operation)
    sample_fields = _select_fields(tokens, aliases) if flat_scope is not None else ()
    aggregate_fields = _aggregate_fields(tokens, aliases)
    limited_sample = flat_scope is not None and "LIMIT" in tokens and bool(sample_fields)
    return SqlReferences(
        parse_status="parsed",
        tables=tuple(sorted(tables)),
        qualified_columns=tuple(sorted(columns)),
        clauses=clauses,
        has_cte=has_cte,
        has_subquery=has_subquery,
        has_window=has_window,
        has_set_operation=has_set_operation,
        flat_scope=flat_scope,
        sample_fields=tuple(sorted(sample_fields)),
        aggregate_fields=tuple(sorted(aggregate_fields)),
        is_limited_sample=limited_sample,
    )


def sample_coverage(sample: SqlReferences, aggregate: SqlReferences) -> tuple[str, ...]:
    """Return only fields proven covered by a later aggregate query.

    The empty result intentionally conflates unsupported and non-matching shapes:
    callers must keep the sample in both cases.
    """

    if (
        sample.parse_status != "parsed"
        or aggregate.parse_status != "parsed"
        or not sample.is_limited_sample
        or sample.flat_scope is None
        or aggregate.flat_scope != sample.flat_scope
    ):
        return ()
    return tuple(sorted(set(sample.sample_fields) & set(aggregate.aggregate_fields)))


def _tokenize(query: str) -> tuple[str, ...]:
    tokens: list[str] = []
    index = 0
    while index < len(query):
        current = query[index]
        following = query[index + 1] if index + 1 < len(query) else ""
        if current.isspace():
            index += 1
        elif current == "-" and following == "-":
            line_end = query.find("\n", index + 2)
            index = len(query) if line_end < 0 else line_end + 1
        elif current == "/" and following == "*":
            block_end = query.find("*/", index + 2)
            if block_end < 0:
                raise ValueError("unterminated block comment")
            index = block_end + 2
        elif current == "'":
            value, index = _quoted_literal(query, index)
            tokens.append(_literal_token("string", value))
        elif current == '"':
            value, index = _quoted_identifier(query, index)
            tokens.append(value)
        elif current == "$":
            match = _DOLLAR_QUOTE.match(query, index)
            if match is None:
                tokens.append(current)
                index += 1
            else:
                end = query.find(match.group(0), match.end())
                if end < 0:
                    raise ValueError("unterminated dollar quote")
                tokens.append(_literal_token("dollar", query[match.end() : end]))
                index = end + len(match.group(0))
        else:
            identifier = _IDENTIFIER.match(query, index)
            if identifier is not None:
                tokens.append(identifier.group(0).upper())
                index = identifier.end()
                continue
            number = _NUMBER.match(query, index)
            if number is not None:
                tokens.append(_literal_token("number", number.group(0)))
                index = number.end()
            else:
                tokens.append(current)
                index += 1
    return tuple(tokens)


def _skip_quoted(query: str, start: int, quote: str, escaped_quote: str) -> int:
    index = start + 1
    while index < len(query):
        if query.startswith(escaped_quote, index):
            index += len(escaped_quote)
        elif query[index] == quote:
            return index + 1
        else:
            index += 1
    raise ValueError("unterminated literal")


def _quoted_identifier(query: str, start: int) -> tuple[str, int]:
    end = _skip_quoted(query, start, '"', '""')
    value = query[start + 1 : end - 1].replace('""', '"')
    return _literal_token("quoted_identifier", value), end


def _quoted_literal(query: str, start: int) -> tuple[str, int]:
    index = start + 1
    parts: list[str] = []
    while index < len(query):
        if query.startswith("''", index):
            parts.append("'")
            index += 2
        elif query[index] == "'":
            return "".join(parts), index + 1
        else:
            parts.append(query[index])
            index += 1
    raise ValueError("unterminated literal")


def _literal_token(kind: str, value: str) -> str:
    return f"<{kind}:{json.dumps(value, ensure_ascii=False)}>"


def _cte_names(tokens: tuple[str, ...]) -> frozenset[str]:
    if not tokens or tokens[0] != "WITH":
        return frozenset()
    names: set[str] = set()
    index = 2 if len(tokens) > 1 and tokens[1] == "RECURSIVE" else 1
    while index + 1 < len(tokens) and _is_identifier(tokens[index]):
        name = tokens[index]
        probe = index + 1
        if probe < len(tokens) and tokens[probe] == "(":
            probe = _matching_parenthesis(tokens, probe) + 1
        if probe >= len(tokens) or tokens[probe] != "AS" or probe + 1 >= len(tokens):
            break
        if tokens[probe + 1] != "(":
            break
        names.add(name)
        index = _matching_parenthesis(tokens, probe + 1) + 1
        if index < len(tokens) and tokens[index] == ",":
            index += 1
            continue
        break
    return frozenset(names)


def _tables_and_aliases(
    tokens: tuple[str, ...], cte_names: frozenset[str]
) -> tuple[set[str], dict[str, str], bool]:
    tables: set[str] = set()
    aliases: dict[str, str] = {}
    has_subquery = False
    for index, token in enumerate(tokens[:-1]):
        if token not in {"FROM", "JOIN"}:
            continue
        position = index + 1
        if tokens[position] == "LATERAL":
            position += 1
        if position >= len(tokens):
            continue
        if tokens[position] == "(":
            has_subquery = True
            continue
        relation, after_relation = _relation_at(tokens, position)
        if relation is None:
            continue
        if relation not in cte_names:
            tables.add(relation)
        alias_position = after_relation
        if alias_position < len(tokens) and tokens[alias_position] == "AS":
            alias_position += 1
        if alias_position < len(tokens) and _is_identifier(tokens[alias_position]):
            alias = tokens[alias_position]
            if alias not in _ALIAS_STOP_WORDS:
                aliases[alias] = relation
        aliases.setdefault(relation.split(".")[-1], relation)
    return tables, aliases, has_subquery


def _relation_at(tokens: tuple[str, ...], start: int) -> tuple[str | None, int]:
    if start >= len(tokens) or not _is_identifier(tokens[start]):
        return None, start
    parts = [tokens[start]]
    index = start + 1
    while index + 1 < len(tokens) and tokens[index] == "." and _is_identifier(tokens[index + 1]):
        parts.append(tokens[index + 1])
        index += 2
    return ".".join(parts), index


def _qualified_columns(
    tokens: tuple[str, ...], aliases: dict[str, str], cte_names: frozenset[str]
) -> set[str]:
    columns: set[str] = set()
    for index in range(len(tokens) - 2):
        qualifier, dot, column = tokens[index : index + 3]
        if dot != "." or not _is_identifier(qualifier) or not _is_identifier(column):
            continue
        base = aliases.get(qualifier)
        if base is not None and base not in cte_names:
            columns.add(f"{base}.{column}")
    return columns


def _flat_scope(
    tokens: tuple[str, ...],
    has_subquery: bool,
    has_cte: bool,
    has_window: bool,
    has_set_operation: bool,
) -> str | None:
    if has_subquery or has_cte or has_window or has_set_operation or "EXPLAIN" in tokens:
        return None
    if any(token in _SAMPLE_UNSUPPORTED for token in tokens):
        return None
    try:
        from_index = tokens.index("FROM")
    except ValueError:
        return None
    end = len(tokens)
    for token in ("ORDER", "LIMIT", "OFFSET", ";"):
        try:
            end = min(end, tokens.index(token, from_index + 1))
        except ValueError:
            continue
    scope = " ".join(tokens[from_index:end]).strip()
    return scope or None


def _select_fields(tokens: tuple[str, ...], aliases: dict[str, str]) -> set[str]:
    try:
        start = tokens.index("SELECT") + 1
        end = tokens.index("FROM", start)
    except ValueError:
        return set()
    fields: set[str] = set()
    for index in range(start, end - 2):
        qualifier, dot, column = tokens[index : index + 3]
        base = aliases.get(qualifier) if dot == "." else None
        if base is not None and _is_identifier(column):
            fields.add(f"{base}.{column}")
    return fields


def _aggregate_fields(tokens: tuple[str, ...], aliases: dict[str, str]) -> set[str]:
    fields: set[str] = set()
    for index in range(len(tokens) - 4):
        if tokens[index].upper() not in _AGGREGATES or tokens[index + 1] != "(":
            continue
        qualifier, dot, column = tokens[index + 2 : index + 5]
        base = aliases.get(qualifier) if dot == "." else None
        if base is not None and _is_identifier(column):
            fields.add(f"{base}.{column}")
    return fields


def _matching_parenthesis(tokens: tuple[str, ...], start: int) -> int:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index] == "(":
            depth += 1
        elif tokens[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return len(tokens) - 1


def _is_identifier(token: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(token)) and token.upper() not in _ALIAS_STOP_WORDS
