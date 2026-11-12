from decimal import Decimal

import pytest

from app.demo import practice_plans
from app.models import Predicate, QueryResult, TimeRange
from app.oracle import SourceOracle, check_rows
from app.session import Session
from app.source import read_source
from app.sql_compiler import CompiledQuery, compile_query
from app.sql_audit import audit_query


def execute(executor, plan):
    session = Session(plan.dataset_id)
    return executor.execute_plan(session, executor.register(session, plan))[0]


def base_plan(executor):
    return practice_plans(executor.manifest)["Q02"].model_copy(update={"time_range": TimeRange(start="2025-04-01", end="2025-05-01")})


def test_group_comparison_and_contribution_match_source(executor):
    plan = base_plan(executor).model_copy(update={"intent": "compare", "dimension": "category", "metric_ids": ["sales", "order_count"],
        "comparison_range": TimeRange(start="2025-03-01", end="2025-04-01")})
    oracle = SourceOracle(plan.dataset_id)
    current = {r["entity_id"]: r for r in oracle.evaluate(plan.model_copy(update={"intent": "aggregate", "comparison_range": None}))}
    previous = {r["entity_id"]: r for r in oracle.evaluate(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None}))}
    result = execute(executor, plan)
    assert result.status == "ok", result.error
    expected = []
    for key in sorted(current.keys() | previous.keys()):
        row = {"entity_id": key, "entity_name": (current.get(key) or previous[key])["entity_name"]}
        for metric in plan.metric_ids:
            c, p = current.get(key, {}).get(metric, 0), previous.get(key, {}).get(metric, 0)
            row.update({metric+"_current": c, metric+"_previous": p, metric+"_change": c-p,
                        metric+"_rate": (c-p)/p if p else None})
        expected.append(row)
    check_rows(result, expected)
    plan = plan.model_copy(update={"intent": "contribution", "metric_ids": ["sales"]})
    result = execute(executor, plan)
    total = sum((r["sales_change"] for r in expected), Decimal(0))
    actual = [dict(zip(result.columns, row)) for row in result.rows]
    assert actual[0]["sales_change"] == max(row["sales_change"] for row in expected)
    assert sum(Decimal(str(row["sales_change"])) for row in actual) == total
    for row in actual:
        assert Decimal(str(row["total_change"])) == total
        assert float(row["contribution_rate"]) == pytest.approx(float(Decimal(str(row["sales_change"]))/total))


def test_numeric_boolean_filters_keep_order_and_item_grains(executor):
    plan = base_plan(executor).model_copy(update={
        "order_where": Predicate(children=[Predicate(field="order_sales", operator="ge", values=[200]),
                                           Predicate(field="review_score", operator="le", values=[2])], conjunction="any"),
        "item_where": Predicate(children=[Predicate(field="price", operator="between", values=[50, 300]),
                                          Predicate(field="seller_id", operator="not_in", values=["no-such-seller"])])})
    source = SourceOracle(plan.dataset_id)
    total = Decimal(0)
    for order in source.orders:
        day = str(order["purchase_at"])[:10]
        if not (order["order_status"] == "delivered" and "2025-04-01" <= day < "2025-05-01"):
            continue
        items = source.items[order["order_id"]]
        amount = sum((Decimal(str(i["price"])) for i in items), Decimal(0))
        review = source.reviews.get(order["order_id"])
        if amount >= 200 or (review is not None and review <= 2):
            total += sum((Decimal(str(i["price"])) for i in items if 50 <= Decimal(str(i["price"])) <= 300), Decimal(0))
    result = execute(executor, plan)
    assert result.status == "ok", result.error
    assert Decimal(str(result.rows[0][0])) == total


def test_explicit_canceled_scope_and_delivery_date(executor):
    source = read_source(executor.manifest["dataset_id"])
    plan = practice_plans(executor.manifest)["Q01"].model_copy(update={"order_statuses": ["cancelled"]})
    result = execute(executor, plan)
    expected = sum(row["order_status"] == "cancelled" for row in source["orders"])
    assert result.rows == [[expected]], result.error
    from app.semantic import catalog
    assert catalog(executor.manifest)["coverage_fields"]["delivered_at"] == executor.manifest["coverage_fields"]["delivered_at"]
    plan = base_plan(executor).model_copy(update={"time_field": "delivered_at", "metric_ids": ["order_count"]})
    result = execute(executor, plan)
    expected = sum(row["order_status"] == "delivered" and row["delivered_at"] is not None and "2025-04-01" <= str(row["delivered_at"])[:10] < "2025-05-01" for row in source["orders"])
    assert result.rows == [[expected]], result.error


def test_order_detail_subtables_do_not_multiply_payments(executor):
    source = SourceOracle(executor.manifest["dataset_id"])
    order = next(order for order in source.orders if order["order_status"] == "delivered" and len(source.items[order["order_id"]]) > 1 and source.payment_rows[order["order_id"]] > 1)
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"intent": "details", "metric_ids": [], "selected_order_id": order["order_id"]})
    result = execute(executor, plan)
    assert result.status == "ok", result.error
    row = dict(zip(result.columns, result.rows[0]))
    assert Decimal(str(row["payment_amount"])) == source.payments[order["order_id"]]
    assert Decimal(str(row["order_sales"])) == sum((Decimal(str(item["price"])) for item in source.items[order["order_id"]]), Decimal(0))
    for level, count in [("items", len(source.items[order["order_id"]])), ("payments", source.payment_rows[order["order_id"]])]:
        result = execute(executor, plan.model_copy(update={"detail_level": level}))
        assert result.status == "ok" and len(result.rows) == count, result.error


def test_diagnostics_reconciliation_is_independent(executor):
    plan = base_plan(executor).model_copy(update={"intent": "diagnose", "metric_ids": [], "diagnosis": "reconciliation"})
    result = execute(executor, plan)
    assert result.status in {"ok", "empty"}, result.error
    oracle = SourceOracle(plan.dataset_id)
    expected = set()
    for order in oracle.orders:
        if order["order_status"] == "delivered" and "2025-04-01" <= str(order["purchase_at"])[:10] < "2025-05-01":
            oid = order["order_id"]
            total = sum((Decimal(str(item["price"]))+Decimal(str(item["freight_value"])) for item in oracle.items[oid]), Decimal(0))
            if abs(oracle.payments[oid]-total) > Decimal('.01'):
                expected.add(oid)
    assert {row[result.columns.index("order_id")] for row in result.rows} == expected


@pytest.mark.parametrize("kind", ["quality", "sales_spike", "seller_outlier"])
def test_diagnostic_queries_run_with_real_data(executor, kind):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"intent": "diagnose", "metric_ids": [], "diagnosis": kind})
    result = execute(executor, plan)
    assert result.status in {"ok", "empty"}, result.error
    if kind == "quality":
        oracle = SourceOracle(plan.dataset_id)
        orders = [o for o in oracle.orders if o["order_status"] == "delivered"]
        pairs = [("delivered_at", "purchase_at"), ("approved_at", "purchase_at"), ("shipped_at", "approved_at"), ("delivered_at", "shipped_at"), ("estimated_delivery_at", "purchase_at")]
        expected_dates = sum(any(o.get(a) and o.get(b) and str(o[a]) < str(o[b]) for a,b in pairs) for o in orders)
        actual = {row[0]: row[result.columns.index("issue_count")] for row in result.rows}
        assert actual["invalid_date"] == expected_dates
        assert actual["duplicate_reviews"] == 0 and actual["negative_amount"] == 0 and actual["missing_timing"] == 0
    elif kind == "sales_spike":
        oracle = SourceOracle(plan.dataset_id)
        monthly = oracle.evaluate(plan.model_copy(update={"intent": "aggregate", "metric_ids": ["sales"], "dimension": "month"}))
        expected = {}
        for previous, current in zip(monthly, monthly[1:]):
            rate = (current["sales"]-previous["sales"])/previous["sales"] if previous["sales"] else None
            if rate is not None and abs(rate) >= Decimal('.3'):
                expected[current["entity_id"]] = rate
        actual = [dict(zip(result.columns, row)) for row in result.rows]
        assert {row["entity_id"] for row in actual} == set(expected)
        for row in actual:
            assert row["change_rate"] == pytest.approx(float(expected[row["entity_id"]]))
    else:
        import statistics
        oracle = SourceOracle(plan.dataset_id)
        sellers = oracle.evaluate(plan.model_copy(update={"intent": "aggregate", "metric_ids": ["delay_rate", "order_count"], "dimension": "seller"}))
        values = [float(row["delay_rate"]) for row in sellers if row["delay_rate"] is not None]
        mean, std = statistics.mean(values), statistics.pstdev(values)
        expected = {row["entity_id"] for row in sellers if std and row["delay_rate"] is not None and (float(row["delay_rate"])-mean)/std >= 2}
        assert {row[0] for row in result.rows} == expected


def test_advanced_audit_rejects_changed_status_and_parameters(executor):
    plan = base_plan(executor).model_copy(update={"order_where": Predicate(field="order_sales", operator="ge", values=[200])})
    query = compile_query(plan)
    with pytest.raises(ValueError, match="口径审计"):
        audit_query(plan, CompiledQuery(query.sql, query.parameters[:-1] + (201,)))


def test_contribution_threshold_does_not_change_net_denominator(executor):
    plan=base_plan(executor).model_copy(update={"intent":"contribution","dimension":"category","metric_ids":["sales"],
        "comparison_range":TimeRange(start="2025-03-01",end="2025-04-01")})
    oracle=SourceOracle(plan.dataset_id)
    current={r["entity_id"]:r for r in oracle.evaluate(plan.model_copy(update={"intent":"aggregate","comparison_range":None,"metric_ids":["sales","order_count"]}))}
    previous={r["entity_id"]:r for r in oracle.evaluate(plan.model_copy(update={"intent":"aggregate","time_range":plan.comparison_range,"comparison_range":None,"metric_ids":["sales","order_count"]}))}
    threshold=sorted(min(current[key]["order_count"],previous[key]["order_count"]) for key in current.keys() & previous.keys())[len(current)//2]
    expected_ids={key for key in current.keys() & previous.keys() if current[key]["order_count"]>=threshold and previous[key]["order_count"]>=threshold}
    net=sum((row["sales"] for row in current.values()),Decimal(0))-sum((row["sales"] for row in previous.values()),Decimal(0))
    result=execute(executor,plan.model_copy(update={"min_group_count":threshold}))
    assert result.status=="ok",result.error
    assert {row[0] for row in result.rows}==expected_ids
    for values in result.rows:
        row=dict(zip(result.columns,values))
        assert Decimal(str(row["total_change"]))==net


def test_full_delivery_scope_uses_its_own_coverage(executor):
    coverage=executor.manifest["coverage_fields"]["delivered_at"]
    plan=practice_plans(executor.manifest)["Q01"].model_copy(update={"time_field":"delivered_at","time_range":TimeRange(**coverage)})
    source=read_source(plan.dataset_id)
    expected=sum(order["order_status"]=="delivered" and bool(order["delivered_at"]) for order in source["orders"])
    result=execute(executor,plan)
    assert result.rows==[[expected]],result.error
