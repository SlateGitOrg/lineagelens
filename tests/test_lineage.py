"""Exact lineage on every annotated fixture. Partial credit is not useful."""

from __future__ import annotations

import unittest

from src.fixtures import CATALOG, DASHBOARDS, DOWNSTREAM, FIXTURES
from src.lineage import Catalog, ColumnRef, LineageGraph, resolve, render_mermaid


def as_strings(refs) -> set[str]:
    return {str(r) for r in refs}


class TestAnnotatedFixtures(unittest.TestCase):
    def test_every_fixture_resolves_exactly(self):
        for model, sql, expected in FIXTURES:
            with self.subTest(model=model):
                got = resolve(sql, model, CATALOG)
                self.assertEqual(
                    {k: as_strings(v) for k, v in got.columns.items()},
                    expected,
                    f"{model}: lineage did not match the annotated answer",
                )

    def test_no_fixture_leaves_anything_unresolved(self):
        for model, sql, _ in FIXTURES:
            with self.subTest(model=model):
                got = resolve(sql, model, CATALOG)
                self.assertEqual(got.unresolved, [], f"{model}: {got.unresolved}")


class TestTheHardCases(unittest.TestCase):
    """The three constructs that make most tools stop at table level."""

    def test_STAR_EXPANSION_against_the_catalog(self):
        got = resolve("select * from customers", "m", CATALOG)
        self.assertEqual(set(got.columns), set(CATALOG.columns_of("customers")))
        self.assertEqual(as_strings(got.columns["email"]), {"customers.email"})

    def test_an_unexpandable_star_is_REPORTED_not_silently_empty(self):
        # The most dangerous possible failure: reporting "no downstream
        # consumers" because the tool could not expand a star.
        got = resolve("select * from mystery_table", "m", Catalog())
        self.assertEqual(got.columns, {})
        self.assertTrue(any("cannot be expanded" in u for u in got.unresolved))

    def test_QUALIFIED_star_expands_only_that_relation(self):
        got = resolve(
            "select c.* from customers c join orders o on o.customer_id = c.id",
            "m", CATALOG)
        self.assertEqual(set(got.columns), set(CATALOG.columns_of("customers")))
        self.assertNotIn("gross_amount", got.columns)

    def test_CTEs_resolve_through_to_the_physical_column(self):
        sql = """
        with a as (select id as k from orders),
             b as (select k from a)
        select k as final from b
        """
        got = resolve(sql, "m", CATALOG)
        self.assertEqual(as_strings(got.columns["final"]), {"orders.id"},
                         "the CTE chain must not stop at the CTE")

    def test_UNION_matches_by_position_not_by_name(self):
        sql = ("select customer_id as entity, gross_amount as amount from orders "
               "union all "
               "select refund_amount, order_id from refunds")
        got = resolve(sql, "m", CATALOG)
        # Position 0 is customer_id / refund_amount. Matching by NAME would
        # pair order_id with customer_id and invert the answer.
        self.assertEqual(as_strings(got.columns["entity"]),
                         {"orders.customer_id", "refunds.refund_amount"})
        self.assertEqual(as_strings(got.columns["amount"]),
                         {"orders.gross_amount", "refunds.order_id"})

    def test_a_self_join_keeps_both_aliases_distinct(self):
        got = resolve(
            "select a.id as x, b.customer_id as y from orders a "
            "join orders b on b.customer_id = a.customer_id", "m", CATALOG)
        self.assertEqual(as_strings(got.columns["x"]), {"orders.id"})
        self.assertEqual(as_strings(got.columns["y"]), {"orders.customer_id"})

    def test_an_expression_follows_EVERY_column_it_references(self):
        got = resolve(
            "select a + b - c as total from mystery", "m",
            Catalog(tables={"mystery": ["a", "b", "c"]}))
        self.assertEqual(as_strings(got.columns["total"]),
                         {"mystery.a", "mystery.b", "mystery.c"})

    def test_function_names_are_not_mistaken_for_columns(self):
        got = resolve("select sum(gross_amount) as t from orders", "m", CATALOG)
        self.assertEqual(as_strings(got.columns["t"]), {"orders.gross_amount"})

    def test_string_literals_are_not_mistaken_for_columns(self):
        got = resolve(
            "select case when channel = 'web' then 'online' else 'other' end "
            "as band from orders", "m", CATALOG)
        self.assertEqual(as_strings(got.columns["band"]), {"orders.channel"})

    def test_comments_do_not_affect_the_result(self):
        sql = """
        -- select id as decoy from orders
        select customer_id /* inline */ from orders
        """
        got = resolve(sql, "m", CATALOG)
        self.assertEqual(set(got.columns), {"customer_id"})


class TestImpactAnalysis(unittest.TestCase):
    def setUp(self):
        self.graph = LineageGraph(dashboards=dict(DASHBOARDS))
        for model, sql, _ in FIXTURES:
            self.graph.add(resolve(sql, model, CATALOG))
        for model, sql in DOWNSTREAM:
            self.graph.add(resolve(sql, model, CATALOG))

    def test_THE_HEADLINE_column_impact_is_small_and_specific(self):
        impacted = self.graph.impact_of(ColumnRef("orders", "discount_pct"))
        models = {m for m, _ in impacted}
        self.assertIn("stg_orders", models)
        self.assertIn("mart_discount_watch", models)
        self.assertNotIn("mart_untouched", models,
                         "a column-level answer must exclude unrelated models")

    def test_THE_CONTRAST_table_level_impact_is_far_larger(self):
        column_models = {m for m, _ in
                         self.graph.impact_of(ColumnRef("orders", "discount_pct"))}
        table_models = set(self.graph.tables_touching("orders"))
        self.assertGreater(
            len(table_models), len(column_models),
            "if table-level and column-level agree, the fixture is too small "
            "to demonstrate the difference",
        )

    def test_impact_is_transitive_through_models(self):
        impacted = self.graph.impact_of(ColumnRef("orders", "gross_amount"))
        models = {m for m, _ in impacted}
        self.assertIn("stg_orders", models)
        self.assertIn("mart_customer_revenue", models,
                      "two hops from the physical column")

    def test_dashboards_affected_are_named(self):
        dashboards = self.graph.dashboards_affected(
            ColumnRef("orders", "discount_pct"))
        self.assertIn("Pricing Governance", dashboards)
        self.assertNotIn("Geo Mix", dashboards)

    def test_an_unused_column_affects_nothing(self):
        self.assertEqual(
            self.graph.impact_of(ColumnRef("orders", "placed_at")), [])

    def test_the_rendered_graph_names_the_origin_and_its_consumers(self):
        mermaid = render_mermaid(self.graph, ColumnRef("orders", "discount_pct"))
        self.assertIn("flowchart LR", mermaid)
        self.assertIn("orders_discount_pct", mermaid)
        self.assertIn("-->", mermaid)


if __name__ == "__main__":
    unittest.main()
