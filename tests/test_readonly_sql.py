"""The text-to-SQL guard: only SELECT on the v_* views may run."""

from __future__ import annotations

import pytest

from conftest import OWNER, PACKER
from order_desk.domain.errors import InvalidInput, NotAllowed
from order_desk.services.reports import run_readonly_query


def test_select_on_a_view_works(deps):
    result = run_readonly_query(deps.db, "SELECT sku, sellable FROM v_stock WHERE category = 'mcb' ORDER BY sku")
    assert result["columns"] == ["sku", "sellable"]
    assert result["row_count"] == 6


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE products SET base_price_paise = 0",
    "INSERT INTO staff(telegram_user_id, name, role) VALUES (1, 'x', 'owner')",
    "DROP TABLE orders",
    "PRAGMA table_info(dealers)",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "SELECT * FROM dealers",                       # base table, not a view
    "SELECT * FROM sqlite_master",
    "SELECT load_extension('x')",
    "SELECT 1; DELETE FROM orders",                # two statements
])
def test_dangerous_or_off_limits_sql_is_refused(deps, sql):
    with pytest.raises(InvalidInput):
        run_readonly_query(deps.db, sql)


def test_data_is_unchanged_after_refused_writes(deps):
    with deps.db.read() as conn:
        before = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    with pytest.raises(InvalidInput):
        run_readonly_query(deps.db, "DELETE FROM orders")
    with deps.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == before


def test_long_query_is_stopped(deps, monkeypatch):
    import order_desk.services.reports as reports
    monkeypatch.setattr(reports, "TIME_LIMIT_S", 0.2)
    slow = ("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
            "SELECT COUNT(*) FROM n, v_stock")
    with pytest.raises(InvalidInput, match="longer than"):
        run_readonly_query(deps.db, slow)


def test_row_limit(deps):
    result = run_readonly_query(deps.db, "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n "
                                         "WHERE x < 500) SELECT x FROM n")
    assert result["row_count"] == 200 and result["truncated"] is True


def test_only_owner_may_run_sql(shop):
    assert shop.run_query(OWNER, "SELECT COUNT(*) AS n FROM v_orders")["rows"] == [{"n": 0}]
    with pytest.raises(NotAllowed):
        shop.run_query(PACKER, "SELECT COUNT(*) FROM v_orders")


def test_chart_is_queued_as_photo(shop, deps):
    result = shop.make_chart(OWNER, "SELECT category, SUM(on_hand) FROM v_stock GROUP BY category", "bar", "Stock")
    assert result["ok"]
    with deps.db.read() as conn:
        row = conn.execute("SELECT kind, chat_id FROM outbox").fetchone()
    assert row["kind"] == "photo" and row["chat_id"] == OWNER


# ------------------------------------------------------------------ LIKE (KI-061)
# SQLite runs `x LIKE y` as a call of the function like(y, x) and the authorizer reports it, so the guard
# refused every name search ("Not allowed: function 'like'"). These are the exact queries the model wrote in
# the 2026-10-27 text2sql eval (evals/results/text2sql/20261027-054454); no model is called here.
_STORED_LIKE_QUERIES = {
    "sellable-switch": "SELECT SUM(sellable) FROM v_stock WHERE name LIKE '%Anchor 6A%'",
    "mcb-sold": ("SELECT SUM(qty) AS mcbs_sold\nFROM v_order_lines\nWHERE product_name LIKE '%MCB%'\n"
                 "  AND order_state IN ('APPROVED', 'PACKING', 'PACKED', 'DISPATCHED')"),
    "finolex-coils": ("SELECT COALESCE(SUM(on_hand), 0) AS total_on_hand\nFROM v_stock\n"
                      "WHERE name LIKE '%Finolex%'"),
}


def _scalar(deps, sql):
    return next(iter(run_readonly_query(deps.db, sql)["rows"][0].values()))


@pytest.mark.parametrize("case", sorted(_STORED_LIKE_QUERIES))
def test_queries_the_model_wrote_with_like_now_run(deps, case):
    result = run_readonly_query(deps.db, _STORED_LIKE_QUERIES[case])
    assert result["row_count"] == 1 and len(result["columns"]) == 1


def test_like_matches_the_same_rows_as_other_ways_to_search(deps):
    assert _scalar(deps, _STORED_LIKE_QUERIES["finolex-coils"]) == \
        _scalar(deps, "SELECT SUM(on_hand) FROM v_stock WHERE brand = 'Finolex'") > 0
    assert _scalar(deps, _STORED_LIKE_QUERIES["sellable-switch"]) == \
        _scalar(deps, "SELECT SUM(sellable) FROM v_stock WHERE instr(name, 'Anchor 6A') > 0") > 0
    assert _scalar(deps, "SELECT COUNT(*) FROM v_stock WHERE name NOT LIKE '%Anchor%'") == \
        _scalar(deps, "SELECT COUNT(*) FROM v_stock WHERE instr(name, 'Anchor') = 0")
    assert _scalar(deps, "SELECT COUNT(*) FROM v_stock WHERE name LIKE '100\\%%' ESCAPE '\\'") == 0


def test_like_does_not_open_any_other_door(deps):
    # LIKE may filter the rows of a view; a pattern that reads a base table or the schema is still refused.
    for sql in ("SELECT sku FROM v_stock WHERE name LIKE (SELECT name FROM dealers LIMIT 1)",
                "SELECT sku FROM v_stock WHERE name LIKE (SELECT sql FROM sqlite_master LIMIT 1)"):
        with pytest.raises(InvalidInput):
            run_readonly_query(deps.db, sql)
