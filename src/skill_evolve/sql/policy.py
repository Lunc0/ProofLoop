"""Conservative lexical guard for candidate SQL before database execution."""

from __future__ import annotations

import re


class SqlPolicyError(ValueError):
    """Candidate SQL falls outside the read-only evaluation policy."""


ROOT_KEYWORDS = frozenset({"SELECT", "WITH", "EXPLAIN"})
BANNED_KEYWORDS = frozenset(
    {
        "ALTER",
        "ATTACH",
        "CALL",
        "CLUSTER",
        "COMMENT",
        "COPY",
        "CREATE",
        "DEALLOCATE",
        "DELETE",
        "DISCARD",
        "DO",
        "DROP",
        "EXECUTE",
        "GRANT",
        "INSERT",
        "INTO",
        "LISTEN",
        "LOCK",
        "MERGE",
        "NOTIFY",
        "PREPARE",
        "REFRESH",
        "REINDEX",
        "RESET",
        "REVOKE",
        "SECURITY",
        "SET",
        "SHOW",
        "START",
        "TRANSACTION",
        "TRUNCATE",
        "UNLISTEN",
        "UPDATE",
        "VACUUM",
    }
)
WORD_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def validate_read_only_sql(query: str) -> None:
    """Reject multi-statement and mutation-capable SQL outside strings/comments.

    The guard is deliberately conservative. PostgreSQL role privileges and a read-only
    transaction are still mandatory defenses because lexical validation is not a
    database security boundary by itself.
    """

    code = _code_only(query)
    _validate_single_statement(code)
    keywords = tuple(match.group(0).upper() for match in WORD_PATTERN.finditer(code))
    if not keywords:
        raise SqlPolicyError("SQL statement is empty")
    if keywords[0] not in ROOT_KEYWORDS:
        raise SqlPolicyError(
            f"only read-only SELECT/WITH/EXPLAIN statements are allowed, got {keywords[0]}"
        )
    banned = next((keyword for keyword in keywords if keyword in BANNED_KEYWORDS), None)
    if banned is not None:
        raise SqlPolicyError(f"read-only policy rejected keyword: {banned}")


def _validate_single_statement(code: str) -> None:
    terminator = code.find(";")
    if terminator < 0:
        return
    if code[terminator + 1 :].strip():
        raise SqlPolicyError("multiple SQL statements are not allowed")


def _code_only(query: str) -> str:
    """Replace literals and comments with spaces while preserving SQL structure."""

    result: list[str] = []
    index = 0
    length = len(query)
    while index < length:
        current = query[index]
        following = query[index + 1] if index + 1 < length else ""
        if current == "-" and following == "-":
            end = query.find("\n", index + 2)
            index = length if end < 0 else end
            result.append(" ")
        elif current == "/" and following == "*":
            end = query.find("*/", index + 2)
            if end < 0:
                raise SqlPolicyError("unterminated block comment")
            index = end + 2
            result.append(" ")
        elif current == "'":
            index = _skip_quoted(query, index, "'", "''")
            result.append(" ")
        elif current == '"':
            index = _skip_quoted(query, index, '"', '""')
            result.append(" ")
        elif current == "$":
            dollar_end = _dollar_quote_end(query, index)
            if dollar_end is None:
                result.append(current)
                index += 1
            else:
                index = dollar_end
                result.append(" ")
        else:
            result.append(current)
            index += 1
    return "".join(result)


def _skip_quoted(query: str, start: int, quote: str, escaped_quote: str) -> int:
    index = start + 1
    while index < len(query):
        if query.startswith(escaped_quote, index):
            index += len(escaped_quote)
        elif query[index] == quote:
            return index + 1
        else:
            index += 1
    raise SqlPolicyError("unterminated quoted SQL literal")


def _dollar_quote_end(query: str, start: int) -> int | None:
    match = re.match(r"\$[A-Za-z_0-9]*\$", query[start:])
    if match is None:
        return None
    delimiter = match.group(0)
    content_start = start + len(delimiter)
    end = query.find(delimiter, content_start)
    if end < 0:
        raise SqlPolicyError("unterminated dollar-quoted SQL literal")
    return end + len(delimiter)
