import pytest

from app.query_engine import QueryError, resolve_date_token
from datetime import date


def test_lists_with_filter_and_total(shop):
    q, _, _ = shop
    r = q.query_records({"table": "products", "filters": [{"column": "stock", "op": "lt", "value": "10"}]})
    assert r["total_rows"] == 2
    assert {row[1] for row in r["rows"]} == {"Novel", "Robot"}


def test_join_and_qualified_columns(shop):
    q, _, _ = shop
    r = q.query_records({"table": "products", "joins": ["categories"],
                         "select": ["products.name", "categories.name"],
                         "filters": [{"column": "categories.name", "op": "eq", "value": "Toys"}]})
    assert [c["name"] for c in r["columns"]] == ["products.name", "categories.name"]
    assert len(r["rows"]) == 2


def test_ambiguous_column_rejected(shop):
    q, _, _ = shop
    with pytest.raises(QueryError, match="ambiguous"):
        q.query_records({"table": "products", "joins": ["categories"], "select": ["name"]})


def test_aggregate_group_and_order(shop):
    q, _, _ = shop
    r = q.aggregate({"table": "orders", "metrics": [{"func": "sum", "column": "total", "alias": "revenue"}],
                     "group_by": [{"column": "status"}], "order_by": [{"column": "revenue", "direction": "desc"}]})
    assert r["rows"][0] == ["paid", 110]
    assert r["metrics"] == ["revenue"]


def test_date_bucket_and_this_month_tokens(shop):
    q, _, _ = shop
    r = q.aggregate({"table": "orders", "metrics": [{"func": "count"}],
                     "filters": [{"column": "placed_at", "op": "gte", "value": "start_of_month"},
                                 {"column": "placed_at", "op": "lt", "value": "start_of_next_month"}]})
    assert r["rows"][0][0] == 2
    r = q.aggregate({"table": "orders", "metrics": [{"func": "count"}], "group_by": [{"column": "placed_at", "bucket": "year"}]})
    assert ["2020", 1] in r["rows"]


def test_pagination(shop):
    q, _, _ = shop
    r = q.query_records({"table": "products", "limit": 2, "offset": 2})
    assert len(r["rows"]) == 2 and r["total_rows"] == 4 and not r["has_more"]


def test_row_limit_is_capped(shop):
    q, _, _ = shop
    assert q.query_records({"table": "products", "limit": 100000})["limit"] == 100


def test_distinct_values(shop):
    q, _, _ = shop
    r = q.distinct_values("orders", "status")
    assert dict((a, b) for a, b in r["rows"]) == {"paid": 2, "cancelled": 1}


@pytest.mark.parametrize("args", [
    {"table": "secrets"},                                                   # table not in allow-list
    {"table": "customers", "select": ["email"]},                            # excluded column
    {"table": "customers", "filters": [{"column": "email", "op": "contains", "value": "a"}]},
    {"table": "products", "select": ["name; DROP TABLE products"]},        # injection via identifier
    {"table": "products; DELETE FROM products"},
    {"table": "products", "joins": ["secrets"]},
    {"table": "products", "order_by": [{"column": "1; --"}]},
    {"table": "products", "unexpected": True},                              # unknown plan fields
    {"table": "products", "filters": [{"column": "stock", "op": "like", "value": "1"}]},  # unapproved operator
    {"table": "products", "filters": [{"column": "stock", "op": "eq", "value": "1 OR 1=1"}]},
])
def test_unsafe_or_invalid_plans_rejected(shop, args):
    q, _, _ = shop
    with pytest.raises(QueryError):
        q.query_records(args)


def test_values_are_bound_not_interpolated(shop):
    q, _, eng = shop
    q.query_records({"table": "products", "filters": [{"column": "name", "op": "eq", "value": "x' OR '1'='1"}]})
    r = q.query_records({"table": "products", "filters": [{"column": "name", "op": "contains", "value": "%"}]})
    assert r["total_rows"] == 0  # LIKE wildcards are escaped


def test_aggregate_type_checks(shop):
    q, _, _ = shop
    with pytest.raises(QueryError):
        q.aggregate({"table": "products", "metrics": [{"func": "sum", "column": "name"}]})
    with pytest.raises(QueryError):
        q.aggregate({"table": "products", "metrics": [{"func": "sum"}]})


def test_describe_table_hides_excluded_columns(shop):
    q, _, _ = shop
    cols = [c["name"] for c in q.describe_table("customers")["columns"]]
    assert "email" not in cols and "name" in cols


def test_date_tokens():
    d = date(2026, 10, 14)
    assert resolve_date_token("start_of_month", d) == date(2026, 10, 1)
    assert resolve_date_token("start_of_next_month", d) == date(2026, 11, 1)
    assert resolve_date_token("7_days_ago", d) == date(2026, 10, 7)


def test_integer_flag_columns_accept_booleans(shop):
    q, _, _ = shop
    r = q.query_records({"table": "products", "filters": [{"column": "stock", "op": "gt", "value": "true"}]})
    assert r["total_rows"] == 4  # true -> 1
