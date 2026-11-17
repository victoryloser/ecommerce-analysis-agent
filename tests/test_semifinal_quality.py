"""构造复赛异常，不代表尚未发布的正式 D1—D4 验收。"""
from copy import deepcopy
from decimal import Decimal
import json

import duckdb
import pytest

from app.import_data import import_tables
from app.models import AnalysisPlan, Predicate, TimeRange
from app.quality import QUERY_SCHEMA, decode_quality
from app.source import SCHEMA
from app.sql_compiler import compile_query
from app.sql_guard import validate_sql
from app.sql_audit import audit_query


@pytest.fixture
def tables():
    data = {name: [] for name in SCHEMA}
    def row(table, **values):
        data[table].append({field: values.get(field) for field in SCHEMA[table]})
    row("customers", customer_id="c", customer_unique_id="u", customer_state="A")
    for key in ("K1", "K2"):
        row("categories", category_id=key, category_name_zh=key)
        row("products", product_id=key, category_id=key)
    row("sellers", seller_id="S1", seller_name="S1")
    for oid, month in (("m1", 3), ("m2", 3), ("a1", 4), ("a2", 4)):
        row("orders", order_id=oid, customer_id="c", order_status="delivered", purchase_at=f"2027-{month:02}-01",
            delivered_at=f"2027-{month:02}-03", estimated_delivery_at=f"2027-{month:02}-04")
        row("order_items", order_id=oid, order_item_id=1, product_id="K1", seller_id="S1", price=100, freight_value=0)
        row("order_payments", order_id=oid, payment_sequence=1, payment_value=100)
    return data


def build(tmp_path, tables, name="fixture"):
    return import_tables(name, tables, [], name, tmp_path)


def query(tmp_path, manifest, **updates):
    plan = AnalysisPlan.model_validate({"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
        "intent": "aggregate", "metric_ids": ["sales"], "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **updates})
    compiled = compile_query(plan)
    validate_sql(compiled.sql, schema=QUERY_SCHEMA, exploration=True)
    audit_query(plan, compiled)
    with duckdb.connect(str(tmp_path / f"{manifest['dataset_id']}.duckdb"), read_only=True) as conn:
        cursor = conn.execute(compiled.sql, compiled.parameters)
        columns, rows, quality = decode_quality([c[0] for c in cursor.description], cursor.fetchall())
    return [dict(zip(columns, row)) for row in rows], quality


def test_missing_payment_never_becomes_zero_growth(tmp_path, tables):
    tables["order_payments"] = [r for r in tables["order_payments"] if r["order_id"] != "a2"]
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, intent="compare", metric_ids=["payment_amount"], comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    assert rows == [{"current_value": None, "previous_value": Decimal(200), "absolute_change": None, "change_rate": None}]
    assert quality[0]["current_value"]["known_value"] == 100
    assert quality[0]["current_value"]["missing_record_count"] == 1


def test_duplicates_conflicts_and_split_payments(tmp_path, tables):
    tables["order_items"].append(deepcopy(tables["order_items"][2]))
    conflict = deepcopy(tables["order_items"][3]); conflict["price"] = 120
    tables["order_items"].append(conflict)
    payment = tables["order_payments"][2]; payment["payment_value"] = 40
    tables["order_payments"].append({**payment, "payment_sequence": 2, "payment_value": 60})
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, metric_ids=["sales", "order_count", "payment_amount"])
    assert rows == [{"sales": None, "order_count": 2, "payment_amount": Decimal(200)}]
    assert quality[0]["sales"]["known_value"] == 100
    assert quality[0]["sales"]["value_unknown_count"] == 1
    with duckdb.connect(str(tmp_path / "fixture.duckdb")) as conn:
        assert conn.execute("SELECT product_id,seller_id,freight_value FROM order_items WHERE order_id='a2'").fetchone() == ("K1", "S1", Decimal(0))
        assert conn.execute("SELECT reason,COUNT(*) FROM quality_issues GROUP BY reason ORDER BY reason").fetchall() == [("amount_conflict", 1), ("exact_duplicate", 1)]
    reversed_tables = {k: list(reversed(v)) for k, v in tables.items()}
    second = build(tmp_path, reversed_tables, "reordered")
    assert query(tmp_path, second, metric_ids=["sales", "order_count", "payment_amount"]) == (rows, quality)


def test_unknown_order_amount_and_existence_preserved(tmp_path, tables):
    tables["order_items"][2]["price"] = 300
    tables["order_items"].append({**tables["order_items"][2], "order_item_id": 2, "price": None})
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, metric_ids=["sales", "order_count"], order_where={"field": "order_sales", "operator": "gt", "values": [400]})
    assert rows == [{"sales": None, "order_count": None}]
    assert quality[0]["order_count"]["membership_unknown_count"] == 1
    rows, quality = query(tmp_path, manifest, metric_ids=["sales", "order_count"], item_where={"field": "price", "operator": "gt", "values": [350]})
    assert rows[0]["order_count"] is None
    assert quality[0]["sales"]["known_count"] == 0


def test_true_order_with_unknown_group_and_three_valued_logic(tmp_path, tables):
    tables["order_items"][2]["price"] = 60
    tables["order_items"].append({**tables["order_items"][2], "order_item_id": 2, "product_id": "K2", "price": None})
    manifest = build(tmp_path, tables)
    predicate = {"field": "price", "operator": "gt", "values": [50]}
    rows, quality = query(tmp_path, manifest, metric_ids=["sales", "order_count"], dimension="category", item_where=predicate)
    assert rows[0]["order_count"] == 2 and rows[0]["sales"] == 160
    assert rows[1]["entity_id"] == "K2" and rows[1]["order_count"] is None
    rows, quality = query(tmp_path, manifest, metric_ids=["sales", "order_count"], item_where=predicate)
    assert rows[0]["order_count"] == 2 and rows[0]["sales"] is None
    false = {"field": "product_id", "values": ["missing"]}
    true = {"field": "product_id", "values": ["K2"]}
    for conjunction, leaf, expected in [("all", false, 0), ("any", true, 2)]:
        rows, _ = query(tmp_path, manifest, metric_ids=["order_count"], item_where={"conjunction": conjunction, "children": [predicate, leaf]})
        assert rows[0]["order_count"] == expected


def test_contribution_denominator_and_ranking_keep_unknowns(tmp_path, tables):
    for r in tables["order_items"]:
        if r["order_id"] in {"m2", "a2"}:
            r["product_id"] = "K2"
    tables["order_items"][2]["price"] = 120
    tables["order_items"][3]["price"] = None
    manifest = build(tmp_path, tables)
    rows, _ = query(tmp_path, manifest, intent="contribution", dimension="category", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    assert rows[0]["sales_change"] == 20
    assert all(r["total_change"] is None and r["contribution_rate"] is None for r in rows)
    rows, _ = query(tmp_path, manifest, intent="rank", dimension="category", top_n=1)
    assert len(rows) == 2 and all(r["rank_no"] is None for r in rows)


def test_repeat_two_known_orders_not_poisoned_by_unknown_third(tmp_path, tables):
    tables["order_items"][3]["price"] = None
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, metric_ids=["repeat_customers"], time_range={"start": "2027-03-01", "end": "2027-05-01"},
                          order_where={"field": "order_sales", "operator": "gt", "values": [50]})
    assert rows == [{"repeat_customers": 1}]
    assert quality[0]["repeat_customers"]["completeness"] == "complete"


def test_quality_join_does_not_multiply_amount_and_detail_is_exact(tmp_path, tables):
    tables["order_items"][2]["price"] = 60
    tables["order_items"].append({**tables["order_items"][2], "order_item_id": 2, "price": 40})
    tables["order_items"].extend([deepcopy(tables["order_items"][2]), deepcopy(tables["order_items"][-1])])
    manifest = build(tmp_path, tables)
    rows, _ = query(tmp_path, manifest)
    assert rows[0]["sales"] == 200
    rows, _ = query(tmp_path, manifest, intent="diagnose", diagnosis="quality", metric_ids=[])
    assert next(r for r in rows if r["entity_id"] == "exact_duplicate")["issue_count"] == 2


@pytest.mark.parametrize("scope", ["aggregate", "compare", "contribution", "rank"])
def test_independent_source_oracle_on_incomplete_data(tmp_path, tables, scope):
    from app.oracle import SourceOracle
    tables["order_items"][3]["price"] = None
    tables["order_items"][3]["product_id"] = "K2"
    tables["order_payments"].pop()
    manifest = build(tmp_path, tables)
    updates = {"intent": scope, "metric_ids": ["sales"], "dimension": "category",
               "order_where": {"field": "order_sales", "operator": "gt", "values": [50]}}
    if scope in {"compare", "contribution"}:
        updates["comparison_range"] = {"start": "2027-03-01", "end": "2027-04-01"}
    plan = AnalysisPlan.model_validate({"dataset_id": "fixture", "metadata_version": manifest["metadata_version"],
        "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **updates})
    actual, _ = query(tmp_path, manifest, **updates)
    expected = SourceOracle("fixture", tables=tables).evaluate(plan)
    assert actual == expected


def test_quality_reaches_preview_chart_pagination_export_and_sql(tmp_path, tables):
    import csv
    from app.executor import Executor
    from app.orchestrator import AnalysisAgent
    from app.session import Session
    from app.presentation import present
    from app.charts import render_charts
    from app.sql_workspace import SQLWorkspace
    tables["order_items"][3].update(product_id="K2", price=None)
    manifest = build(tmp_path, tables)
    plan = AnalysisPlan(dataset_id="fixture", metadata_version=manifest["metadata_version"], intent="aggregate",
        metric_ids=["sales"], dimension="category", time_range=TimeRange(start="2027-04-01", end="2027-05-01"))
    class Provider:
        model = "offline"
        def complete(self, messages):
            return plan.model_dump_json()
    executor = Executor("fixture", tmp_path)
    agent = AnalysisAgent(Provider(), {"fixture": executor})
    session = Session("fixture")
    turn = agent.respond(session, "quality outputs")
    view = present(turn.evidence)
    assert "总值未知" in view.table[1][-1]
    html = render_charts(turn.evidence, ["sales"])
    assert "K2" in html and "未知 / 不可计算" in html
    exported = agent.export(session)
    with open(exported["csv_file"], encoding="utf-8-sig", newline="") as handle:
        records = list(csv.DictReader(handle))
    assert records[1]["sales"] == "" and json.loads(records[1]["__quality"])["sales"]["completeness"] == "unknown"
    page = agent.page(session, 0, 1)
    assert page.evidence["result"]["quality"][0]["sales"]["completeness"] == "complete"
    next_page = agent.page(session, 1, 1)
    assert next_page.evidence["result"]["quality"][0]["sales"]["completeness"] == "unknown"
    workspace = SQLWorkspace({"fixture": executor})
    sql = workspace.execute(session, "SELECT * FROM analysis_result", [], "analysis_result")
    assert len(sql["result"]["rows"]) == 2 and sql["result"]["quality"][1]["sales"]["completeness"] == "unknown"
    issues = agent.quality_details(session)
    assert issues.evidence["result"]["status"] == "ok"


def test_null_group_key_is_not_an_absent_group(tmp_path, tables):
    tables["customers"][0]["customer_state"] = None
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, intent="compare", dimension="customer_state", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    assert rows[0]["entity_id"] is None
    assert rows[0]["sales_current"] == rows[0]["sales_previous"] == 200
    assert quality[0]["sales_current"]["completeness"] == "complete"


def test_uncertain_order_threshold_keeps_candidate(tmp_path, tables):
    tables["order_items"][3]["price"] = None
    manifest = build(tmp_path, tables)
    rows, quality = query(tmp_path, manifest, intent="compare", dimension="category", min_group_count=2,
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"}, order_where={"field": "order_sales", "operator": "gt", "values": [50]})
    assert len(rows) == 1 and rows[0]["current_orders"] is None
    assert rows[0]["sales_change"] is None


@pytest.mark.parametrize("level", ["orders", "items", "payments", "reviews", "quality", "reconciliation"])
def test_detail_and_quality_oracle_is_independent(tmp_path, tables, level):
    from app.oracle import SourceOracle
    tables["order_items"][3]["price"] = None
    tables["order_payments"].pop()
    manifest = build(tmp_path, tables)
    options = {"intent": "details", "detail_level": level, "metric_ids": []} if level in {"orders", "items", "payments", "reviews"} else {"intent": "diagnose", "diagnosis": level, "metric_ids": []}
    plan = AnalysisPlan.model_validate({"dataset_id": "fixture", "metadata_version": manifest["metadata_version"],
        "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **options})
    rows, _ = query(tmp_path, manifest, **options)
    assert rows == SourceOracle("fixture", tables=tables).evaluate(plan)
