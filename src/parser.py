"""A focused SQL parser for column-level lineage.

THE DIFFERENTIATOR LIVES HERE.

Table-level lineage says "47 downstream objects touch this table". Nobody can
act on that. Column-level lineage says "three objects read this specific
column, here they are" - which is a code review comment.

The reason most tools stop at table level is that the interesting cases are
hard: `SELECT *` has to be expanded against the source schema, CTEs have to be
resolved before the outer query can be, and a UNION matches columns by POSITION
rather than by name. Getting those three right is the entire value; a lineage
tool that is correct 90% of the time cannot be trusted for impact analysis at
all, because you cannot tell which 10% you are looking at.

Production uses SQLGlot, which embeds real dialect grammars. This is a focused
subset covering the constructs that appear in analytics SQL, written so the
resolution rules are inspectable rather than buried in a dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ColumnRef:
    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


@dataclass
class Relation:
    """A table or CTE, and the columns it exposes."""

    name: str
    columns: list[str] = field(default_factory=list)
    #: For a CTE: where each exposed column came from.
    lineage: dict[str, set[ColumnRef]] = field(default_factory=dict)


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return sql


def _split_top_level(text: str, separator: str) -> list[str]:
    """Split on a separator that is not inside parentheses."""
    out: list[str] = []
    depth = 0
    current = ""
    i = 0
    sep = separator.lower()
    lowered = text.lower()
    while i < len(text):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and lowered.startswith(sep, i):
            # Word-boundary checks apply to KEYWORD separators (from, union).
            # Applying them to punctuation is wrong: a comma is preceded by an
            # alphanumeric character in every select list ever written, so the
            # boundary check rejects every split and the whole clause comes
            # back as one item.
            if not sep[0].isalpha():
                before_ok = after_ok = True
            else:
                before_ok = i == 0 or not (
                    lowered[i - 1].isalnum() or lowered[i - 1] == "_")
                after = i + len(sep)
                after_ok = after >= len(text) or not (
                    lowered[after].isalnum() or lowered[after] == "_")
            if before_ok and after_ok:
                out.append(current)
                current = ""
                i += len(sep)
                continue
        current += ch
        i += 1
    out.append(current)
    return out


def _match_paren(text: str, start: int) -> int:
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


# ---------------------------------------------------------------------------


@dataclass
class SelectItem:
    expression: str
    alias: str | None
    is_star: bool
    star_qualifier: str | None = None


def parse_select_items(select_clause: str) -> list[SelectItem]:
    items: list[SelectItem] = []
    for raw in _split_top_level(select_clause, ","):
        expr = raw.strip()
        if not expr:
            continue

        alias = None
        m = re.search(r"\s+as\s+([A-Za-z_]\w*)\s*$", expr, re.I)
        if m:
            alias = m.group(1)
            expr = expr[: m.start()].strip()
        else:
            # Implicit alias: `col_a alias` with no AS keyword.
            m2 = re.match(r"^([A-Za-z_][\w.]*)\s+([A-Za-z_]\w*)$", expr)
            if m2 and m2.group(2).lower() not in {"as"}:
                expr, alias = m2.group(1), m2.group(2)

        if expr == "*":
            items.append(SelectItem(expr, alias, True, None))
        elif expr.endswith(".*"):
            items.append(SelectItem(expr, alias, True, expr[:-2]))
        else:
            items.append(SelectItem(expr, alias, False))
    return items


def extract_columns(expression: str) -> list[tuple[str | None, str]]:
    """Every column reference inside an expression.

    An expression can reference several columns - `a.x + b.y`, `coalesce(p, q)`
    - and lineage that only follows the first one is silently incomplete.
    """
    cleaned = re.sub(r"'[^']*'", " ", expression)
    out: list[tuple[str | None, str]] = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\b|\b([A-Za-z_]\w*)\b",
                         cleaned):
        if m.group(1):
            out.append((m.group(1), m.group(2)))
            continue
        word = m.group(3)
        nxt = cleaned[m.end():].lstrip()
        if nxt.startswith("("):
            continue                      # a function name, not a column
        if word.lower() in SQL_KEYWORDS:
            continue
        out.append((None, word))
    return out


SQL_KEYWORDS = {
    "select", "from", "where", "join", "left", "right", "inner", "outer",
    "full", "on", "as", "and", "or", "not", "case", "when", "then", "else",
    "end", "group", "by", "order", "having", "union", "all", "distinct",
    "with", "null", "is", "in", "like", "between", "cast", "over",
    "partition", "asc", "desc", "limit", "cross", "using", "true", "false",
}


@dataclass
class FromSource:
    name: str
    alias: str


def parse_from(from_clause: str) -> list[FromSource]:
    text = re.sub(r"\s+", " ", from_clause).strip()
    text = re.sub(
        r"\b(left|right|inner|outer|full|cross)\s+join\b", " JOIN ", text, flags=re.I)
    text = re.sub(r"\bjoin\b", " JOIN ", text, flags=re.I)

    parts: list[str] = []
    for chunk in text.split("JOIN"):
        chunk = re.split(r"\bon\b|\busing\b", chunk, flags=re.I)[0]
        parts.extend(p for p in _split_top_level(chunk, ",") if p.strip())

    sources: list[FromSource] = []
    for part in parts:
        tokens = part.strip().split()
        if not tokens:
            continue
        name = tokens[0].strip()
        alias = name.split(".")[-1]
        if len(tokens) >= 3 and tokens[1].lower() == "as":
            alias = tokens[2]
        elif len(tokens) >= 2 and tokens[1].lower() not in SQL_KEYWORDS:
            alias = tokens[1]
        sources.append(FromSource(name, alias))
    return sources


@dataclass
class ParsedQuery:
    ctes: list[tuple[str, str]]
    select_clause: str
    from_clause: str
    #: Branches of a UNION, if any. Matched by POSITION, not by name.
    union_branches: list[str] = field(default_factory=list)


def parse_query(sql: str) -> ParsedQuery:
    sql = _strip_comments(sql).strip().rstrip(";")

    ctes: list[tuple[str, str]] = []
    if re.match(r"^\s*with\b", sql, re.I):
        rest = re.sub(r"^\s*with\s+", "", sql, flags=re.I)
        while True:
            m = re.match(r"\s*([A-Za-z_]\w*)\s+as\s*\(", rest, re.I)
            if not m:
                break
            open_paren = rest.index("(", m.start())
            close = _match_paren(rest, open_paren)
            ctes.append((m.group(1), rest[open_paren + 1:close]))
            rest = rest[close + 1:].lstrip()
            if rest.startswith(","):
                rest = rest[1:]
                continue
            break
        sql = rest

    branches = _split_top_level(sql, "union")
    branches = [re.sub(r"^\s*all\b", "", b, flags=re.I).strip() for b in branches]
    main = branches[0]

    select_clause, from_clause = _split_select_from(main)
    return ParsedQuery(
        ctes=ctes, select_clause=select_clause, from_clause=from_clause,
        union_branches=branches[1:],
    )


def _split_select_from(sql: str) -> tuple[str, str]:
    body = re.sub(r"^\s*select\s+(distinct\s+)?", "", sql.strip(), flags=re.I)
    parts = _split_top_level(body, "from")
    if len(parts) < 2:
        return body.strip(), ""
    select_clause = parts[0]
    remainder = "from".join(parts[1:])
    remainder = re.split(
        r"\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b",
        remainder, flags=re.I)[0]
    return select_clause.strip(), remainder.strip()
