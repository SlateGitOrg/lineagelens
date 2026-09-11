"""The 60-second artefact: 3 dashboards, not 47 objects.

Run: python -m src.demo
"""

from __future__ import annotations

from .fixtures import CATALOG, DASHBOARDS, DOWNSTREAM, FIXTURES
from .lineage import ColumnRef, LineageGraph, render_mermaid, resolve

TARGET = ColumnRef("orders", "discount_pct")


def main() -> None:
    graph = LineageGraph(dashboards=dict(DASHBOARDS))
    for model, sql, _ in FIXTURES:
        graph.add(resolve(sql, model, CATALOG))
    for model, sql in DOWNSTREAM:
        graph.add(resolve(sql, model, CATALOG))

    total_columns = sum(len(m.columns) for m in graph.models.values())
    print("\n  LINEAGELENS - 'which three dashboards break?'")
    print("  " + "=" * 72)
    print(f"  {len(graph.models)} models, {total_columns} output columns, "
          f"{len(DASHBOARDS)} dashboards.\n")

    print(f"  An engineer proposes renaming `{TARGET}`.\n")

    table_level = graph.tables_touching("orders")
    print("  WHAT A TABLE-LEVEL TOOL REPORTS")
    print("  " + "-" * 72)
    print(f"    {len(table_level)} objects touch `orders`:")
    print(f"    {', '.join(table_level)}")
    print("    Now go and read all of them.\n")

    impacted = graph.impact_of(TARGET)
    print("  WHAT COLUMN-LEVEL LINEAGE REPORTS")
    print("  " + "-" * 72)
    print(f"    {len(impacted)} columns read it, transitively:")
    for model, column in impacted:
        print(f"      {model}.{column}")

    dashboards = graph.dashboards_affected(TARGET)
    print(f"\n    Dashboards affected: {', '.join(dashboards)}")
    others = sorted(set(sum(DASHBOARDS.values(), [])) - set(dashboards))
    print(f"    Unaffected:          {', '.join(others)}\n")

    print("  THE CASES THAT MAKE THIS HARD")
    print("  " + "-" * 72)
    star = resolve("select * from customers", "demo_star", CATALOG)
    print(f"    SELECT *          expanded to {len(star.columns)} columns "
          f"against the catalog")

    cte = resolve(
        "with a as (select id as k from orders), b as (select k from a) "
        "select k as final from b", "demo_cte", CATALOG)
    print(f"    CTE chain         final -> "
          f"{', '.join(str(r) for r in cte.columns['final'])}")

    union = resolve(
        "select customer_id as entity, gross_amount as amount from orders "
        "union all select refund_amount, order_id from refunds",
        "demo_union", CATALOG)
    print("    UNION             matched by POSITION:")
    for name in ("entity", "amount"):
        print(f"                        {name} <- "
              f"{', '.join(sorted(str(r) for r in union.columns[name]))}")
    print("                      (name-matching would invert this)")

    unknown = resolve("select * from mystery", "demo_unknown", CATALOG)
    print(f"\n    Unexpandable star REPORTED, not silently empty:")
    for u in unknown.unresolved:
        print(f"                        {u}")
    print("    Reporting 'no downstream consumers' because the tool could not")
    print("    expand a star is the most dangerous answer it could give.\n")

    print("  Rendered impact graph:\n")
    print(render_mermaid(graph, TARGET).split("\n")[0])
    for line in render_mermaid(graph, TARGET).split("\n")[1:8]:
        print("    " + line)
    print()


if __name__ == "__main__":
    main()
