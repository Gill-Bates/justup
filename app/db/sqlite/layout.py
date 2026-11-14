#!/usr/bin/env python3
#
# app/db/sqlite/layout.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Structural schema layout reading and comparison used by the compatibility check."""

from __future__ import annotations

import functools
import re
import sqlite3
from collections.abc import Iterable
from typing import Any, NamedTuple

from .schema import SCHEMA_DRIFT, SCHEMA_VERSION, IncompatibleSchemaError, _create_baseline_schema
from .schema_v1 import V1_DDL


class _TableShape(NamedTuple):
    """Order-independent structure of one table (PRAGMA-derived; no row data)."""

    columns: dict[str, tuple[str, int, str | None, int]]  # name -> (type, not null, default, pk position)
    foreign_keys: frozenset[tuple[Any, ...]]
    unique_keys: frozenset[tuple[str, ...]]  # column tuples of UNIQUE and PRIMARY KEY indexes
    checks: frozenset[str]  # normalised CHECK expressions


class _IndexShape(NamedTuple):
    table: str
    unique: bool
    partial: bool
    columns: tuple[str, ...]


_Layout = tuple[dict[str, _TableShape], dict[str, _IndexShape]]


# Documented historical variants that stay accepted although they differ from the baseline DDL:
# - alerts.active_incident: former version-10 databases may carry DEFAULT 0 while the new baseline
#   carries DEFAULT 1. Every writer sets the column explicitly, so the default never applies.
# - idx_recipients_phone: a former-current database may keep the non-unique/non-partial index while duplicate
#   phone numbers exist (_ensure_unique_recipient_phone); only its table and columns are compared.
_TOLERATED_COLUMN_DEFAULTS: dict[tuple[str, str], frozenset[str]] = {
    ("alerts", "active_incident"): frozenset({"0", "1"}),
}


_TOLERATED_INDEX_VARIANTS = frozenset({"idx_recipients_phone"})


# Stored versions whose layout is the frozen version-1 layout (1 itself and the former-current stamp 10).
_VERSION1_LAYOUT_STAMPS = frozenset({1, 10})


_SQLITE_CORRUPT_CODES = frozenset({11, 26})  # SQLITE_CORRUPT, SQLITE_NOTADB


_MAX_REPORTED_DIFFS = 6


_SQL_TOKEN = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|`(?:[^`]|``)*`|\s+|[^\s'"`]+""")


def _normalise_sql(expression: str) -> str:
    """Lower-case and de-space an SQL fragment outside quoted parts so equal checks compare equal."""
    out: list[str] = []
    pending_space = False
    for token in _SQL_TOKEN.findall(expression):
        if token.isspace():
            pending_space = bool(out)
            continue
        part = token if token[0] in "'\"`" else token.lower()
        glue = (
            bool(out)
            and (out[-1][-1].isalnum() or out[-1][-1] in "_'\"`")
            and (part[0].isalnum() or part[0] in "_'\"`")
        )
        if pending_space and glue:
            out.append(" ")
        out.append(part)
        pending_space = False
    return "".join(out)


def _skip_quoted(sql: str, start: int) -> int:
    """Return the index after the quoted string/identifier opening at *start* (doubled quotes escape)."""
    quote = sql[start]
    i = start + 1
    while i < len(sql):
        if sql[i] == quote:
            if sql[i + 1 : i + 2] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _check_expressions(sql: str | None) -> frozenset[str]:
    """Extract the normalised CHECK (...) expressions of a CREATE TABLE statement."""
    found: set[str] = set()
    text = sql or ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i = _skip_quoted(text, i)
        elif text.startswith("--", i):
            i = text.find("\n", i) if "\n" in text[i:] else len(text)
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
        elif ch.isalpha() or ch == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word, i = text[i:j], j
            if word.upper() == "CHECK":
                k = i
                while k < len(text) and text[k].isspace():
                    k += 1
                if k < len(text) and text[k] == "(":
                    depth, m = 0, k
                    while m < len(text):
                        if text[m] in "'\"`":
                            m = _skip_quoted(text, m)
                            continue
                        depth += text[m] == "("
                        depth -= text[m] == ")"
                        m += 1
                        if depth == 0:
                            break
                    found.add(_normalise_sql(text[k + 1 : m - 1]))
                    i = m
        else:
            i += 1
    return frozenset(found)


def _layout(conn: sqlite3.Connection) -> _Layout:
    """Read the application tables and indexes of *conn* (read-only, independent of column order)."""
    tables: dict[str, _TableShape] = {}
    sql_by_table = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        )
    }
    indexes: dict[str, _IndexShape] = {}
    for name, sql in sql_by_table.items():
        columns = {
            r[1]: ((r[2] or "").upper(), int(r[3]), None if r[4] is None else str(r[4]).strip(), int(r[5]))
            for r in conn.execute("SELECT * FROM pragma_table_info(?)", (name,))
        }
        foreign_keys: dict[int, list[Any]] = {}
        for r in conn.execute(
            'SELECT id, "table", "from", "to", on_update, on_delete FROM pragma_foreign_key_list(?) ORDER BY id, seq',
            (name,),
        ):
            entry = foreign_keys.setdefault(r[0], [r[1], [], r[4], r[5]])
            entry[1].append((r[2], r[3]))
        unique_keys: set[tuple[str, ...]] = set()
        for r in conn.execute('SELECT name, "unique", origin, partial FROM pragma_index_list(?)', (name,)):
            index_columns = tuple(
                (x[0] or "<expression>") + (" DESC" if x[1] else "")
                for x in conn.execute(
                    "SELECT name, desc FROM pragma_index_xinfo(?) WHERE key = 1 ORDER BY seqno", (r[0],)
                )
            )
            if r[2] == "c":
                indexes[r[0]] = _IndexShape(name, bool(r[1]), bool(r[3]), index_columns)
            else:
                unique_keys.add(index_columns)
        tables[name] = _TableShape(
            columns,
            frozenset((e[0], tuple(e[1]), e[2], e[3]) for e in foreign_keys.values()),
            frozenset(unique_keys),
            _check_expressions(sql),
        )
    return tables, indexes


@functools.cache
def _expected_layout() -> _Layout:
    """Table and index structure of the current schema, derived from the one DDL definition."""
    reference = sqlite3.connect(":memory:")
    try:
        _create_baseline_schema(reference)
        return _layout(reference)
    finally:
        reference.close()


@functools.cache
def _version1_layout() -> _Layout:
    """Table and index structure of schema version 1 (also the former version-10 stamp), from its frozen DDL."""
    reference = sqlite3.connect(":memory:")
    try:
        for statement in V1_DDL:
            reference.execute(statement)
        return _layout(reference)
    finally:
        reference.close()


def normalise_trigger_sql(sql: str | None) -> str:
    """Comparable form of a stored trigger definition (case, spacing and ``IF NOT EXISTS`` are irrelevant)."""
    return _normalise_sql(re.sub(r"\bIF\s+NOT\s+EXISTS\b", "", sql or "", flags=re.IGNORECASE))


@functools.cache
def _expected_triggers() -> dict[str, str]:
    reference = sqlite3.connect(":memory:")
    try:
        _create_baseline_schema(reference)
        return {
            name: normalise_trigger_sql(sql)
            for name, sql in reference.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'trigger'")
        }
    finally:
        reference.close()


def expected_triggers() -> dict[str, str]:
    """Triggers the current schema defines (name -> normalised definition); anything else is foreign."""
    return dict(_expected_triggers())


def _layout_for_version(version: int) -> _Layout:
    """Expected layout of a stored schema version: the frozen version-1 layout for 1 and 10, else the current."""
    if version != SCHEMA_VERSION and version in _VERSION1_LAYOUT_STAMPS:
        return _version1_layout()
    return _expected_layout()


def _describe_column(column: tuple[str, int, str | None, int]) -> str:
    type_, notnull, default, pk = column
    return " ".join(
        part
        for part in (
            type_ or "(no type)",
            "NOT NULL" if notnull else "",
            f"DEFAULT {default}" if default else "",
            "PK" if pk else "",
        )
        if part
    )


def _short(items: Iterable[str]) -> str:
    shown = sorted(items)
    return ", ".join(s if len(s) <= 60 else s[:57] + "..." for s in shown[:3]) + (", ..." if len(shown) > 3 else "")


def _diff_layout(actual: _Layout, expected: _Layout) -> list[str]:
    """Return human-readable structural differences (column order is deliberately not compared).

    Tables or indexes that the baseline does not know are ignored; only names, types and rules appear
    in the output, never row contents.
    """
    actual_tables, actual_indexes = actual
    expected_tables, expected_indexes = expected
    diffs: list[str] = []
    for table, want in expected_tables.items():
        have = actual_tables.get(table)
        if have is None:
            diffs.append(f"table {table}: missing")
            continue
        if missing := set(want.columns) - set(have.columns):
            diffs.append(f"table {table}: missing column(s) {', '.join(sorted(missing))}")
        if extra := set(have.columns) - set(want.columns):
            diffs.append(f"table {table}: unexpected column(s) {', '.join(sorted(extra))}")
        for column in sorted(set(want.columns) & set(have.columns)):
            expected_column, found = want.columns[column], have.columns[column]
            if expected_column == found:
                continue
            allowed = _TOLERATED_COLUMN_DEFAULTS.get((table, column), frozenset())
            same_apart_from_default = found[:2] + found[3:] == expected_column[:2] + expected_column[3:]
            if same_apart_from_default and {found[2] or "", expected_column[2] or ""} <= allowed:
                continue
            diffs.append(
                f"table {table}: column {column} is [{_describe_column(found)}], "
                f"expected [{_describe_column(expected_column)}]"
            )
        if have.foreign_keys != want.foreign_keys:
            diffs.append(f"table {table}: foreign keys differ")
        if have.unique_keys != want.unique_keys:
            diffs.append(f"table {table}: UNIQUE/PRIMARY KEY constraints differ")
        if have.checks != want.checks:
            detail = []
            if lost := want.checks - have.checks:
                detail.append(f"missing {_short(lost)}")
            if added := have.checks - want.checks:
                detail.append(f"unexpected {_short(added)}")
            diffs.append(f"table {table}: CHECK constraints differ ({'; '.join(detail)})")
    for index, want_index in sorted(expected_indexes.items()):
        have_index = actual_indexes.get(index)
        if have_index is None:
            diffs.append(f"index {index}: missing")
        elif index in _TOLERATED_INDEX_VARIANTS:
            if (have_index.table, have_index.columns) != (want_index.table, want_index.columns):
                diffs.append(f"index {index}: differs from the expected definition")
        elif have_index != want_index:
            diffs.append(f"index {index}: differs from the expected definition")
    return diffs


def _database_file(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    return (row[2] if row else "") or "<in-memory>"


_SAFE_HINT = (
    "The file was not modified and JustUp never deletes data. Back up the data directory "
    "(data/sqlite and data/tsdb) before any recovery step; see docs: Database maintenance > "
    "Schema baseline and versioning."
)


def _layout_problems(conn: sqlite3.Connection) -> list[str]:
    return _diff_layout(_layout(conn), _expected_layout())


def _drift_error(
    conn: sqlite3.Connection, version: int, diffs: list[str], *, expected_version: int = SCHEMA_VERSION
) -> IncompatibleSchemaError:
    shown = "; ".join(diffs[:_MAX_REPORTED_DIFFS]) + (
        f"; ... (+{len(diffs) - _MAX_REPORTED_DIFFS} more)" if len(diffs) > _MAX_REPORTED_DIFFS else ""
    )
    return IncompatibleSchemaError(
        f"Cannot use database {_database_file(conn)}: its layout differs from schema version {expected_version} "
        f"(stored version {version}): {shown}. Column order is not compared. {_SAFE_HINT}",
        kind=SCHEMA_DRIFT,
        version=version,
    )
