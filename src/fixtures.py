"""Annotated SQL fixtures. The expected lineage is written down FIRST.

These are the specification. Partial credit is not useful for a lineage tool:
if it is right 90% of the time you cannot tell which 10% you are looking at,
so every awkward case below has an exact expected answer.
"""

from __future__ import annotations

from .lineage import Catalog

CATALOG = Catalog(tables={
    "orders": ["id", "customer_id", "placed_at", "gross_amount",
               "discount_pct", "channel"],
    "customers": ["id", "name", "email", "country", "signup_at"],
    "refunds": ["id", "order_id", "refund_amount", "refunded_at"],
    "channels": ["code", "label"],
})

#: (model name, sql, expected {output column: {source columns}})
FIXTURES: list[tuple[str, str, dict[str, set[str]]]] = [
    (
        "stg_orders",
        """
        select
            id as order_id,
            customer_id,
            gross_amount,
            discount_pct,
            gross_amount * (1 - discount_pct) as net_amount
        from orders
        """,
        {
            "order_id": {"orders.id"},
            "customer_id": {"orders.customer_id"},
            "gross_amount": {"orders.gross_amount"},
            "discount_pct": {"orders.discount_pct"},
            # An expression referencing TWO columns. Following only the first
            # is a silent, common bug.
            "net_amount": {"orders.gross_amount", "orders.discount_pct"},
        },
    ),
    (
        "stg_customers_star",
        # SELECT * must be expanded against the catalog.
        "select * from customers",
        {
            "id": {"customers.id"},
            "name": {"customers.name"},
            "email": {"customers.email"},
            "country": {"customers.country"},
            "signup_at": {"customers.signup_at"},
        },
    ),
    (
        "qualified_star",
        "select c.*, o.gross_amount from customers c join orders o "
        "on o.customer_id = c.id",
        {
            "id": {"customers.id"},
            "name": {"customers.name"},
            "email": {"customers.email"},
            "country": {"customers.country"},
            "signup_at": {"customers.signup_at"},
            "gross_amount": {"orders.gross_amount"},
        },
    ),
    (
        "cte_chain",
        # A CTE must be resolved before the outer query, and the outer query
        # must see THROUGH it to the physical column.
        """
        with base as (
            select id as order_id, gross_amount, discount_pct
            from orders
        ),
        priced as (
            select order_id, gross_amount * (1 - discount_pct) as net
            from base
        )
        select order_id, net as net_amount from priced
        """,
        {
            "order_id": {"orders.id"},
            "net_amount": {"orders.gross_amount", "orders.discount_pct"},
        },
    ),
    (
        "self_join",
        # Two aliases onto the same table. Attributing both to one alias is a
        # classic resolution failure.
        "select a.id as parent_id, b.id as child_id "
        "from orders a join orders b on b.customer_id = a.customer_id",
        {
            "parent_id": {"orders.id"},
            "child_id": {"orders.id"},
        },
    ),
    (
        "union_by_position",
        # UNION matches by POSITION. Note the second branch reverses the
        # columns: name-matching would produce exactly the wrong answer.
        """
        select customer_id as entity, gross_amount as amount from orders
        union all
        select refund_amount, order_id from refunds
        """,
        {
            "entity": {"orders.customer_id", "refunds.refund_amount"},
            "amount": {"orders.gross_amount", "refunds.order_id"},
        },
    ),
    (
        "coalesce_expression",
        "select coalesce(c.email, c.name) as contact from customers c",
        {"contact": {"customers.email", "customers.name"}},
    ),
    (
        "case_expression",
        """
        select
            case when o.discount_pct > 0.3 then 'deep' else o.channel end
                as channel_band
        from orders o
        """,
        {"channel_band": {"orders.discount_pct", "orders.channel"}},
    ),
    (
        "aggregate_with_group_by",
        "select customer_id, sum(gross_amount) as total from orders "
        "group by customer_id",
        {
            "customer_id": {"orders.customer_id"},
            "total": {"orders.gross_amount"},
        },
    ),
    (
        "implicit_alias",
        "select o.gross_amount amount from orders o",
        {"amount": {"orders.gross_amount"}},
    ),
]

#: Downstream models built on the staging models above, plus the dashboards
#: that read them. This is what makes impact analysis answerable.
DOWNSTREAM: list[tuple[str, str]] = [
    (
        "mart_customer_revenue",
        """
        select
            customer_id,
            sum(net_amount) as revenue,
            count(order_id) as order_count
        from stg_orders
        group by customer_id
        """,
    ),
    (
        "mart_discount_watch",
        "select order_id, discount_pct from stg_orders where discount_pct > 0.2",
    ),
    (
        "mart_untouched",
        "select id, country from customers",
    ),
]

DASHBOARDS: dict[str, list[str]] = {
    "mart_customer_revenue": ["Exec Revenue Summary", "CS Account Health"],
    "mart_discount_watch": ["Pricing Governance"],
    "mart_untouched": ["Geo Mix"],
    "stg_orders": ["Ops Daily Orders"],
}
