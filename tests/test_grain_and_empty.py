import hashlib
import json
from decimal import Decimal

import duckdb
import pytest

from app.executor import Executor
from app.models import AnalysisPlan, TimeRange
from app.semantic import PlanError, validate_plan
from app.session import Session
from app.source import SCHEMA


@pytest.fixture(scope="module")
def small_executor(tmp_path_factory):
    """特制业务样本：同人异客户ID、多明细、多支付、多商家、缺时间。"""
    directory = tmp_path_factory.mktemp("grain")
    path = directory / "csv_small.duckdb"
    with duckdb.connect(str(path)) as conn:
        for table, fields in SCHEMA.items():
            conn.execute(f"CREATE TABLE {table} ({','.join(f'{name} {kind}' for name, kind in fields.items())})")
        conn.execute("INSERT INTO customers (customer_id, customer_unique_id, customer_state) VALUES ('C1','U1','A'),('C2','U1','A'),('C3','U2','B')")
        conn.execute("INSERT INTO categories (category_id,category_name_zh) VALUES ('cat','测试品类')")
        conn.execute("INSERT INTO products (product_id,category_id) VALUES ('P1','cat')")
        conn.execute("INSERT INTO sellers (seller_id,seller_name) VALUES ('S1','商家一'),('S2','商家二')")
        conn.execute("""INSERT INTO orders (order_id,customer_id,order_status,purchase_at,delivered_at,estimated_delivery_at) VALUES
            ('O1','C1','delivered','2025-04-01','2025-04-04','2025-04-03'),
            ('O2','C2','delivered','2025-04-02','2025-04-03','2025-04-03'),
            ('O3','C3','delivered','2025-04-03',NULL,'2025-04-04'),
            ('O4','C3','canceled','2025-04-03',NULL,'2025-04-04')""")
        conn.execute("""INSERT INTO order_items (order_id,order_item_id,product_id,seller_id,price) VALUES
            ('O1',1,'P1','S1',10),('O1',2,'P1','S1',20),('O1',3,'P1','S2',30),
            ('O2',1,'P1','S1',40),('O3',1,'P1','S2',0),('O4',1,'P1','S2',999)""")
        conn.execute("INSERT INTO order_payments (order_id,payment_sequence,payment_value) VALUES ('O1',1,25),('O1',2,35),('O2',1,40),('O4',1,999)")
        conn.execute("INSERT INTO order_reviews (review_id,order_id,review_score) VALUES ('R1','O1',2),('R2','O2',4)")
    manifest = {
        "dataset_id": "csv_small", "name": "业务边界测试", "metadata_version": "1.0.0",
        "database_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "coverage": {"start": "2025-01-01", "end": "2025-07-01"},
        "entities": {"categories": [["cat", "测试品类"]], "sellers": [["S1", "商家一"], ["S2", "商家二"]], "customer_states": ["A", "B"]},
        "quality": {"duplicate_review_orders": 0, "delivered_missing_timing": 1},
    }
    (directory / "csv_small.manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return Executor("csv_small", directory)


def query(executor, **updates):
    fields = {"dataset_id": "csv_small", "metadata_version": "1.0.0", "intent": "aggregate", "metric_ids": ["sales"], "time_range": {"start": "2025-04-01", "end": "2025-05-01"}}
    plan = AnalysisPlan.model_validate(fields | updates)
    session = Session("csv_small")
    result, _ = executor.execute_plan(session, executor.register(session, plan))
    assert result.status in {"ok", "empty"}, result.error
    return result


def test_multi_item_multi_payment_stays_at_order_grain(small_executor):
    result = query(small_executor, metric_ids=["sales", "order_count", "payment_amount"])
    assert result.rows == [[Decimal("100.00"), 3, Decimal("100.00")]]


def test_seller_orders_are_deduplicated_but_sales_keep_ownership(small_executor):
    result = query(small_executor, metric_ids=["sales", "order_count"], dimension="seller")
    assert [(r[0], r[2], r[3]) for r in result.rows] == [("S1", Decimal("70.00"), 2), ("S2", Decimal("30.00"), 2)]


def test_customer_unique_id_merges_customer_ids(small_executor):
    assert query(small_executor, metric_ids=["repeat_customers"]).rows == [[1]]


def test_missing_timing_is_not_counted_as_on_time(small_executor):
    result = query(small_executor, metric_ids=["delay_rate"])
    assert result.rows == [[None, 3, 1, 1]]


def test_zero_is_distinct_from_no_group(small_executor):
    empty_time = {"start": "2025-02-01", "end": "2025-03-01"}
    total = query(small_executor, time_range=empty_time)
    grouped = query(small_executor, dimension="seller", time_range=empty_time)
    assert total.status == "ok" and total.rows == [[Decimal("0.00")]]
    assert grouped.status == "empty" and grouped.rows == []


def test_zero_base_gives_undefined_rate(small_executor):
    result = query(small_executor, intent="compare", comparison_range={"start": "2025-03-01", "end": "2025-04-01"})
    assert result.rows == [[Decimal("100.00"), Decimal("0.00"), Decimal("100.00"), None]]


def test_tied_first_place_is_retained(small_executor):
    result = query(small_executor, intent="rank", metric_ids=["order_count"], dimension="seller", top_n=1)
    assert [(r[0], r[2], r[3]) for r in result.rows] == [("S1", 2, 1), ("S2", 2, 1)]


def test_seller_filter_preserves_sales_and_distinct_orders(small_executor):
    result = query(small_executor, metric_ids=["sales", "order_count"], filters=[{"field": "seller_id", "values": ["S1"]}])
    assert result.rows == [[Decimal("70.00"), 2]]


def test_unreviewed_orders_do_not_reduce_review_average(small_executor):
    result = query(small_executor, metric_ids=["avg_review", "low_score_rate"])
    assert result.rows == [[3.0, 0.5, 2]]


def test_duplicate_reviews_require_a_defined_business_rule(small_executor):
    manifest = dict(small_executor.manifest, quality={"duplicate_review_orders": 1})
    plan = AnalysisPlan(dataset_id="csv_small", metadata_version="1.0.0", intent="aggregate", metric_ids=["avg_review"], time_range=TimeRange(start="2025-04-01", end="2025-05-01"))
    with pytest.raises(PlanError, match="一单多评价"):
        validate_plan(plan, manifest)
