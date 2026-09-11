"""Resolve column-level lineage, including the three hard cases."""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import (
    ColumnRef, extract_columns, parse_from, parse_query, parse_select_items,
    _split_select_from,
)


@dataclass
class Catalog:
    """Known physical tables and their columns.

    Star expansion is impossible without this. A lineage tool with no catalog
    quietly treats `SELECT *` as contributing nothing, and then reports that
    the column you asked about has no downstream consumers - the most dangerous
    possible answer, because it looks like good news.
    """

    tables: dict[str, list[str]] = field(default_factory=dict)

    def columns_of(self, table: str) -> list[str]:
        key = table.split(".")[-1]
        return self.tables.get(key, self.tables.get(table, []))

    def knows(self, table: str) -> bool:
        return bool(self.columns_of(table))


@dataclass
class ModelLineage:
    model: str
    #: output column -> the source columns that feed it
    columns: dict[str, set[ColumnRef]] = field(default_factory=dict)
    #: constructs the resolver could not follow, reported rather than dropped
    unresolved: list[str] = field(default_factory=list)


def _resolve_branch(
    select_clause: str, from_clause: str, catalog: Catalog,
    scopes: dict[str, dict[str, set[ColumnRef]]],
) -> tuple[dict[str, set[ColumnRef]], list[str], list[str]]:
    """Resolve one SELECT. Returns (lineage, ordered output names, unresolved)."""
    sources = parse_from(from_clause)
    alias_to_name = {s.alias: s.name for s in sources}
    lineage: dict[str, set[ColumnRef]] = {}
    order: list[str] = []
    unresolved: list[str] = []

    def sources_for(column: str, qualifier: str | None) -> set[ColumnRef]:
        """Which source does this unqualified column come from?"""
        if qualifier:
            name = alias_to_name.get(qualifier, qualifier)
            upstream = scopes.get(name)
            if upstream is not None:
                return set(upstream.get(column, {ColumnRef(name, column)}))
            return {ColumnRef(name, column)}

        # Unqualified: find the source that exposes it. If several do, the SQL
        # would be ambiguous and the database would reject it; if none is
        # known, record it rather than guessing.
        matches: set[ColumnRef] = set()
        for s in sources:
            upstream = scopes.get(s.name)
            if upstream is not None:
                if column in upstream:
                    matches |= upstream[column]
                continue
            if catalog.knows(s.name):
                if column in catalog.columns_of(s.name):
                    matches.add(ColumnRef(s.name, column))
            else:
                matches.add(ColumnRef(s.name, column))
        if not matches:
            unresolved.append(f"could not attribute column `{column}`")
        return matches

    for item in parse_select_items(select_clause):
        if item.is_star:
            # STAR EXPANSION. Skipping this is why most tools stop at table
            # level: an unexpanded star makes every downstream column invisible.
            targets = (
                [s for s in sources if s.alias == item.star_qualifier
                 or s.name == item.star_qualifier]
                if item.star_qualifier else sources
            )
            for s in targets:
                upstream = scopes.get(s.name)
                if upstream is not None:
                    for col, refs in upstream.items():
                        lineage.setdefault(col, set()).update(refs)
                        if col not in order:
                            order.append(col)
                elif catalog.knows(s.name):
                    for col in catalog.columns_of(s.name):
                        lineage.setdefault(col, set()).add(ColumnRef(s.name, col))
                        if col not in order:
                            order.append(col)
                else:
                    unresolved.append(
                        f"SELECT * from unknown relation `{s.name}` - its "
                        f"columns cannot be expanded")
            continue

        refs: set[ColumnRef] = set()
        for qualifier, column in extract_columns(item.expression):
            refs |= sources_for(column, qualifier)

        name = item.alias or item.expression.split(".")[-1]
        lineage.setdefault(name, set()).update(refs)
        if name not in order:
            order.append(name)

    return lineage, order, unresolved


def resolve(sql: str, model: str, catalog: Catalog) -> ModelLineage:
    parsed = parse_query(sql)
    scopes: dict[str, dict[str, set[ColumnRef]]] = {}
    unresolved: list[str] = []

    # CTEs must be resolved before the outer query, in declaration order.
    for name, body in parsed.ctes:
        select_clause, from_clause = _split_select_from(body)
        cte_lineage, _, cte_unresolved = _resolve_branch(
            select_clause, from_clause, catalog, scopes)
        scopes[name] = cte_lineage
        unresolved.extend(f"in CTE {name}: {u}" for u in cte_unresolved)

    lineage, order, main_unresolved = _resolve_branch(
        parsed.select_clause, parsed.from_clause, catalog, scopes)
    unresolved.extend(main_unresolved)

    # UNION branches match by POSITION, not by name. Matching by name is the
    # obvious implementation and it is wrong: `SELECT a, b UNION SELECT b, a`
    # is legal, and name-matching would invert the lineage.
    for branch in parsed.union_branches:
        b_select, b_from = _split_select_from(branch)
        b_lineage, b_order, b_unresolved = _resolve_branch(
            b_select, b_from, catalog, scopes)
        unresolved.extend(b_unresolved)
        for position, out_name in enumerate(order):
            if position < len(b_order):
                lineage.setdefault(out_name, set()).update(
                    b_lineage.get(b_order[position], set()))

    return ModelLineage(model, lineage, unresolved)


# ---------------------------------------------------------------------------
# The graph and impact analysis
# ---------------------------------------------------------------------------

@dataclass
class LineageGraph:
    models: dict[str, ModelLineage] = field(default_factory=dict)
    #: model -> dashboards that read it
    dashboards: dict[str, list[str]] = field(default_factory=dict)

    def add(self, lineage: ModelLineage) -> None:
        self.models[lineage.model] = lineage

    def impact_of(self, ref: ColumnRef) -> list[tuple[str, str]]:
        """Transitive downstream (model, column) pairs reading this column."""
        found: set[tuple[str, str]] = set()
        frontier = {ref}
        seen: set[ColumnRef] = set()

        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            for model, lineage in self.models.items():
                for out_column, refs in lineage.columns.items():
                    if current in refs and (model, out_column) not in found:
                        found.add((model, out_column))
                        frontier.add(ColumnRef(model, out_column))
        return sorted(found)

    def dashboards_affected(self, ref: ColumnRef) -> list[str]:
        out: set[str] = set()
        for model, _ in self.impact_of(ref):
            out.update(self.dashboards.get(model, []))
        return sorted(out)

    def tables_touching(self, table: str) -> list[str]:
        """What a TABLE-level tool would report. Included for contrast."""
        out: set[str] = set()
        for model, lineage in self.models.items():
            for refs in lineage.columns.values():
                if any(r.table.split(".")[-1] == table.split(".")[-1]
                       for r in refs):
                    out.add(model)
        # Table-level lineage is transitive too, which is why its answers get
        # so large so quickly.
        changed = True
        while changed:
            changed = False
            for model, lineage in self.models.items():
                if model in out:
                    continue
                if any(r.table in out for refs in lineage.columns.values()
                       for r in refs):
                    out.add(model)
                    changed = True
        return sorted(out)


def render_mermaid(graph: LineageGraph, ref: ColumnRef) -> str:
    lines = ["flowchart LR"]
    node = lambda c: str(c).replace(".", "_")  # noqa: E731
    lines.append(f'  {node(ref)}["{ref}"]:::origin')
    for model, column in graph.impact_of(ref):
        target = ColumnRef(model, column)
        lines.append(f'  {node(target)}["{target}"]')
    for model, lineage in graph.models.items():
        for out_column, refs in lineage.columns.items():
            for r in refs:
                if (model, out_column) in graph.impact_of(ref) or r == ref:
                    lines.append(f"  {node(r)} --> {node(ColumnRef(model, out_column))}")
    lines.append("  classDef origin fill:#fde68a,stroke:#b45309;")
    return "\n".join(dict.fromkeys(lines))
