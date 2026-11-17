"""Synthetic monetary-policy tests; no competition data or reference answers."""
from copy import deepcopy
from decimal import Decimal
import csv
import json

import duckdb
import pytest

from app.calculation import CalculationContext, LEGACY_RULE, ROUND2_RULE
from app.import_data import import_tables
from app.executor import Executor
from app.models import AnalysisPlan, QueryResult
from app.oracle import SourceOracle, check_rows
from app.quality import QUERY_SCHEMA, decode_quality, quality_note
from app.result_math import resolve_ratios
from app.semantic import catalog, get_catalog, validate_plan
from app.session import Session
from app.session_store import SessionStore
from app.sql_audit import audit_query
from app.sql_compiler import compile_query
from app.sql_guard import validate_sql
from app.source import SCHEMA
from app.snapshot_diff import compare_snapshots
from app.answers import answer, cell


def source_tables(conflict=True):
    data = {table: [] for table in SCHEMA}
    def add(table, **values):
        data[table].append({field: values.get(field) for field in SCHEMA[table]})
    add("customers", customer_id="C", customer_unique_id="U", customer_state="X")
    for key in ("A", "B"):
        add("categories", category_id=key, category_name_zh=key)
        add("products", product_id=key, category_id=key, product_name=key)
        add("sellers", seller_id=key, seller_name=key)
    for oid, month in (("M", 3), ("O1", 4), ("O2", 4)):
        add("orders", order_id=oid, customer_id="C", order_status="delivered", purchase_at=f"2027-{month:02}-01",
            delivered_at=f"2027-{month:02}-02", estimated_delivery_at=f"2027-{month:02}-03")
    for oid, number, category, price, freight in (("M", 1, "B", 40, 4), ("O1", 1, "A", 100, 10),
                                                ("O1", 2, "B", 50, 5), ("O2", 1, "B", 40, 4)):
        add("order_items", order_id=oid, order_item_id=number, product_id=category, seller_id=category,
            price=price, freight_value=freight)
    add("order_payments", order_id="M", payment_sequence=1, payment_value=44)
    add("order_payments", order_id="O1", payment_sequence=1, payment_value=100)
    add("order_payments", order_id="O1", payment_sequence=2, payment_value=65)
    if conflict:
        data["order_items"].append({**data["order_items"][1], "price": 120})
    return data


@pytest.fixture(scope="module")
def snapshots(tmp_path_factory):
    directory = tmp_path_factory.mktemp("round2")
    manifests = {}
    for name, conflict, rule in (("formal", True, ROUND2_RULE), ("legacy", True, LEGACY_RULE), ("clean", False, ROUND2_RULE)):
        manifests[name] = import_tables(name, source_tables(conflict), [], name, directory, quality_rule_version=rule)
    return directory, manifests


def plan_for(manifest, **changes):
    return AnalysisPlan.model_validate({"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
        "intent": "aggregate", "metric_ids": ["sales", "freight", "order_count"],
        "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **changes})


def direct(directory, manifest, **changes):
    plan = plan_for(manifest, **changes)
    context = CalculationContext.from_manifest(manifest)
    validate_plan(plan, manifest)
    compiled = compile_query(plan, context)
    validate_sql(compiled.sql, schema=QUERY_SCHEMA, exploration=True)
    audit_query(plan, compiled, context)
    with duckdb.connect(str(directory / f"{manifest['dataset_id']}.duckdb"), read_only=True) as conn:
        cursor = conn.execute(compiled.sql, compiled.parameters)
        columns, rows, quality = decode_quality([c[0] for c in cursor.description], cursor.fetchall())
    ratios = resolve_ratios(columns, rows, compiled.ratios)
    result = QueryResult(query_id="synthetic", status="ok" if rows else "empty", columns=columns, rows=rows, quality=quality, ratios=ratios)
    return plan, result


@pytest.mark.parametrize("changes", [{}, {"filters": [{"field": "category_id", "values": ["B"]}]},
    {"filters": [{"field": "seller_id", "values": ["B"]}]}, {"item_where": {"field": "product_id", "values": ["B"]}}])
def test_whole_order_excluded_before_item_filter(snapshots, changes):
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], **changes)
    assert result.rows == [[Decimal(40), Decimal(4), 2]]
    for metric in ("sales", "freight"):
        q = result.quality[0][metric]
        assert q["policy_eligible_order_count"] == 1 and q["excluded_order_count"] == 1
        assert q["calculation_status"] == "determined" and not q["population_complete"]
        assert "总值未知" not in quality_note({metric: q})
    check_rows(result, SourceOracle("formal", source_tables(), data_dir=directory).evaluate(plan, with_quality=True))


@pytest.mark.parametrize("group", [{"dimension": "category"}, {"dimension": "seller"},
    {"group_by": ["category", "seller"]}, {"group_by": ["month", "category"]}])
def test_group_amounts_roll_up_but_orders_do_not(snapshots, group):
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], **group)
    data = [dict(zip(result.columns, row)) for row in result.rows]
    assert sum(r["sales"] for r in data) == 40
    assert sum(r["freight"] for r in data) == 4
    assert sum(r["order_count"] for r in data) == 3
    assert any(r["sales"] == 0 and r["order_count"] == 1 for r in data)
    check_rows(result, SourceOracle("formal", source_tables(), data_dir=directory).evaluate(plan, with_quality=True))


def test_unknown_membership_only_blocks_relevant_metric(snapshots):
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], order_where={"field": "order_sales", "operator": "lt", "values": [160]})
    assert result.rows == [[Decimal(40), Decimal(4), None]]
    assert result.quality[0]["sales"]["excluded_order_count"] == 0
    assert result.quality[0]["sales"]["excluded_candidate_order_count"] == 1
    assert result.quality[0]["order_count"]["calculation_status"] == "insufficient_data"
    check_rows(result, SourceOracle("formal", source_tables(), data_dir=directory).evaluate(plan, with_quality=True))


def test_fact_freight_and_payment_are_independent(snapshots):
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], intent="details", metric_ids=[], selected_order_id="O1",
                          order_where={"field": "order_freight", "operator": "gt", "values": [0]})
    row = dict(zip(result.columns, result.rows[0]))
    assert row["order_sales"] is None and row["order_freight"] == 15
    assert row["payment_amount"] == 165 and row["amount_eligible"] is False
    check_rows(result, SourceOracle("formal", source_tables(), data_dir=directory).evaluate(plan))
    _, aggregate = direct(directory, manifests["formal"], metric_ids=["sales", "freight", "payment_amount"])
    assert aggregate.rows == [[Decimal(40), Decimal(4), None]]


@pytest.mark.parametrize("field,other,expected", [("price", None, [None, Decimal(19)]),
    ("freight_value", 12, [Decimal(190), None]), ("price", "100.00", [Decimal(190), Decimal(19)])])
def test_only_distinct_nonnull_prices_trigger_exclusion(tmp_path, field, other, expected):
    tables = source_tables(False)
    tables["order_items"].append({**tables["order_items"][1], field: other})
    manifest = import_tables("boundary", tables, [], "boundary", tmp_path, quality_rule_version=ROUND2_RULE)
    plan, result = direct(tmp_path, manifest, metric_ids=["sales", "freight"])
    assert result.rows == [expected]
    assert result.quality[0]["sales"]["excluded_order_count"] == 0
    check_rows(result, SourceOracle("boundary", tables, data_dir=tmp_path).evaluate(plan, with_quality=True))


def test_reordering_duplicates_do_not_change_policy(tmp_path):
    tables = source_tables()
    tables["order_items"].append(deepcopy(tables["order_items"][1]))
    first = import_tables("one", tables, [], "one", tmp_path, quality_rule_version=ROUND2_RULE)
    reversed_tables = {name: list(reversed(rows)) for name, rows in tables.items()}
    second = import_tables("two", reversed_tables, [], "two", tmp_path, quality_rule_version=ROUND2_RULE)
    a, b = [direct(tmp_path, m)[1] for m in (first, second)]
    assert a.rows == b.rows and a.quality == b.quality


def test_zero_no_orders_and_all_excluded_are_distinct(snapshots):
    directory, manifests = snapshots
    _, empty = direct(directory, manifests["formal"], time_range={"start": "2027-04-10", "end": "2027-04-20"})
    _, excluded = direct(directory, manifests["formal"], filters=[{"field": "category_id", "values": ["A"]}])
    assert empty.rows == [[Decimal(0), Decimal(0), 0]]
    assert excluded.rows == [[Decimal(0), Decimal(0), 1]]
    assert empty.quality[0]["sales"]["population_complete"]
    assert not excluded.quality[0]["sales"]["population_complete"]
    assert excluded.quality[0]["sales"]["calculation_status"] == "determined"


def test_unknown_threshold_prevents_rank_even_when_money_known(snapshots):
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], intent="rank", dimension="category", min_group_count=2,
                          order_where={"field": "order_sales", "operator": "lt", "values": [160]})
    row = dict(zip(result.columns, result.rows[0]))
    assert row["sales"] == 40 and row["rank_no"] is None
    assert result.quality[0]["sales"]["group_eligibility"] == "uncertain"
    check_rows(result, SourceOracle("formal", source_tables(), data_dir=directory).evaluate(plan, with_quality=True))


def test_known_scope_rank_can_bind_workflow(snapshots):
    from app.workflow import bind_step
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], intent="rank", dimension="category", top_n=1)
    next_step = plan_for(manifests["formal"], source_step=1, bind_field="category_id")
    step, status, _ = bind_step(next_step, [{"status": "completed", "evidence": {"plan": plan.model_dump(), "result": result.model_dump()}}])
    assert status == "pending" and step.filters[0].values == ["B"]


def test_zero_denominators_are_explicit(snapshots):
    directory, manifests = snapshots
    _, result = direct(directory, manifests["formal"], intent="contribution", metric_ids=["sales"], dimension="category",
                       comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    assert all(r["contribution_rate"]["calculation_status"] == "undefined" for r in result.ratios)
    assert sum(row[result.columns.index("sales_change")] for row in result.rows) == 0


def test_ratio_precision_survives_execution_restore_and_full_csv(tmp_path):
    from app.query_worker import run_query
    tables = source_tables(False)
    tables["order_items"] = [tables["order_items"][0], tables["order_items"][-1]]
    tables["order_items"][-1]["price"] = "40.21"
    manifest = import_tables("ratio", tables, [], "ratio", tmp_path, quality_rule_version=ROUND2_RULE)
    executor = Executor("ratio", tmp_path)
    plan = plan_for(manifest, intent="compare", metric_ids=["sales"], comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    session = Session("ratio")
    result, compiled = executor.execute_plan(session, executor.register(session, plan))
    assert cell("change_rate", result.rows[0][3], plan) == "0.53%"
    assert result.ratios[0]["change_rate"]["numerator"] == Decimal("0.21")
    loaded = QueryResult.model_validate_json(result.model_dump_json())
    assert loaded.rows == result.rows and loaded.ratios == result.ratios
    session.last_result, session.last_successful_plan = result, plan
    store = SessionStore(tmp_path / "session.sqlite3")
    store.save(session, manifest)
    assert store.load(session.session_id, manifest).last_result.rows == result.rows
    path = tmp_path / "full.csv"
    exported = run_query(executor.path, compiled, export_path=path)
    assert exported.status == "ok"
    row = next(csv.DictReader(path.open(encoding="utf-8-sig")))
    assert Decimal(row["change_rate"]) == Decimal("0.00525")
    assert "0.53%" in answer(plan, loaded, manifest)


def test_legacy_rule_and_catalog_remain_bound(snapshots):
    directory, manifests = snapshots
    for name, expected in (("formal", [Decimal(40), Decimal(4), 2]), ("legacy", [None, Decimal(19), 2])):
        plan, result = direct(directory, manifests[name])
        assert result.rows == [expected]
        details = get_catalog(manifests[name], plan)
        assert details["quality_rule_version"] == manifests[name]["quality_rule_version"]
        assert catalog(manifests[name])["metrics"]["sales"]["amount_basis"] == ("policy_eligible" if name == "formal" else "full_population")
    with pytest.raises(ValueError, match="不支持的金额规则"):
        validate_plan(plan_for(manifests["formal"]), {**manifests["formal"], "quality_rule_version": "unknown"})
    plan = plan_for(manifests["formal"])
    with pytest.raises(ValueError, match="口径审计"):
        audit_query(plan, compile_query(plan), CalculationContext(ROUND2_RULE))


@pytest.mark.parametrize("reverse", [False, True])
def test_snapshot_bridge_excludes_every_item_once(snapshots, tmp_path, reverse):
    directory, manifests = snapshots
    names = ["clean", "formal"] if not reverse else ["formal", "clean"]
    plans = [plan_for(manifests[n]) for n in names]
    report = compare_snapshots([directory / f"{n}.duckdb" for n in names], plans, tmp_path / "diff")
    by_metric = {r["metric"]: r for r in report["metric_comparisons"]}
    sign = 1 if reverse else -1
    assert by_metric["sales"]["delta"] == sign * 150
    assert by_metric["freight"]["delta"] == sign * 15
    assert by_metric["order_count"]["delta"] == 0
    for m, value in (("sales", 150), ("freight", 15)):
        entry = by_metric[m]
        bridge = entry["membership"]
        assert entry["reconciled"] and bridge["common_change"] == 0
        assert bridge["eligibility_entered_value" if reverse else "eligibility_exited_value"] == value
        assert entry["before"]["state"] == entry["after"]["state"] == "known"


def test_cross_rule_snapshot_diff_never_claims_revision(snapshots, tmp_path):
    directory, manifests = snapshots
    names = ["legacy", "formal"]
    report = compare_snapshots([directory / f"{n}.duckdb" for n in names], [plan_for(manifests[n]) for n in names], tmp_path / "diff")
    assert all(not row["comparable"] and row["delta"] is None for row in report["metric_comparisons"])


@pytest.mark.parametrize("name,expected", [("formal", [Decimal(40), Decimal(4), 2]), ("legacy", [None, Decimal(19), 2])])
def test_restored_query_workspace_and_export_bind_same_rule(snapshots, tmp_path, name, expected):
    from app.sql_workspace import compile_exploration
    from app.query_worker import run_query
    directory, manifests = snapshots
    executor = Executor(name, directory)
    plan = plan_for(manifests[name])
    session = Session(name)
    result, compiled = executor.execute_plan(session, executor.register(session, plan))
    assert result.rows == [expected]
    session.last_result, session.last_successful_plan = result, plan
    session.analysis_evidence = {"record_id": "test", "type": "query"}
    store = SessionStore(tmp_path / "sessions.sqlite3")
    store.save(session, manifests[name])
    restored = store.load(session.session_id, manifests[name])
    repeated, _ = executor.execute_plan(restored, executor.register(restored, restored.last_successful_plan))
    assert repeated.rows == [expected]
    query, _ = compile_exploration(restored, executor, "SELECT * FROM analysis_result", [], "analysis_result")
    assert run_query(executor.path, query).rows == [expected]
    path = tmp_path / "export.csv"
    assert run_query(executor.path, compiled, export_path=path).status == "ok"
    with path.open(encoding="utf-8-sig") as handle:
        row = next(csv.DictReader(handle))
    assert (Decimal(row["sales"]) if row["sales"] else None) == expected[0]
    assert Decimal(row["freight"]) == expected[1]


def test_upload_defaults_to_formal_rule_without_filename_detection(tmp_path):
    from app.data_upload import discover, suggest_mapping, import_upload
    files = []
    for table, rows in source_tables().items():
        path = tmp_path / (table + ".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(SCHEMA[table]))
            writer.writeheader()
            writer.writerows(rows)
        files.append(str(path))
    directory = tmp_path / "data"
    manifest = import_upload(files, suggest_mapping(discover(files)), "unrelated-name", directory)
    assert manifest["quality_rule_version"] == ROUND2_RULE
    assert direct(directory, manifest)[1].rows == [[Decimal(40), Decimal(4), 2]]


@pytest.mark.parametrize("n,d,formatted,status", [("0.21", "40", "0.53%", "determined"),
    ("-0.21", "40", "-0.53%", "determined"), ("0", "0", "未定义（分母为0）", "undefined"),
    (None, "40", "数据不足", "insufficient_data")])
def test_ratio_status_and_rounding(n, d, formatted, status):
    from app.result_math import ratio_value
    value, actual = ratio_value(n, d)
    assert actual == status
    assert cell("change_rate", value, None, {"change_rate": {"calculation_status": status}}) == formatted


def test_large_amounts_preserve_cents(tmp_path):
    tables = source_tables(False)
    for row in tables["order_items"]:
        row["price"] = "9999999999999999.99"
    manifest = import_tables("large", tables, [], "large", tmp_path, quality_rule_version=ROUND2_RULE)
    plan, result = direct(tmp_path, manifest)
    assert result.rows[0][0] == Decimal("29999999999999999.97")
    assert cell("sales", result.rows[0][0], plan) == "29999999999999999.97"
    check_rows(result, SourceOracle("large", tables, data_dir=tmp_path).evaluate(plan, with_quality=True))


@pytest.mark.parametrize("intent", ["compare", "contribution"])
def test_two_period_threshold_uncertainty_remains_visible(tmp_path, intent):
    tables = source_tables()
    tables["orders"].append({**tables["orders"][0], "order_id": "M2"})
    tables["order_items"].append({**tables["order_items"][0], "order_id": "M2", "price": 50})
    manifest = import_tables("threshold", tables, [], "threshold", tmp_path, quality_rule_version=ROUND2_RULE)
    plan, result = direct(tmp_path, manifest, intent=intent, metric_ids=["sales"], dimension="category", min_group_count=2,
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"},
        order_where={"field": "order_sales", "operator": "lt", "values": [160]})
    assert len(result.rows) == 1
    row = dict(zip(result.columns, result.rows[0]))
    assert row["sales_change"] == -50
    assert result.quality[0]["sales_current"]["group_eligibility"] == "uncertain"
    check_rows(result, SourceOracle("threshold", tables, data_dir=tmp_path).evaluate(plan, with_quality=True))


def test_scope_and_amount_changes_have_disjoint_bridge_events(tmp_path):
    old, new = source_tables(False), source_tables()
    new["order_items"][3].update(price=45, freight_value=6)
    manifests = [import_tables(name, tables, [], name, tmp_path, quality_rule_version=ROUND2_RULE)
                 for name, tables in (("before", old), ("after", new))]
    report = compare_snapshots([tmp_path / (m["dataset_id"] + ".duckdb") for m in manifests],
                               [plan_for(m) for m in manifests], tmp_path / "diff")
    for row in report["metric_comparisons"]:
        assert row["reconciled"]
        if row["metric"] == "sales":
            assert row["delta"] == -145
            assert row["membership"]["common_change"] == 5
            assert row["membership"]["eligibility_exited_value"] == 150
        if row["metric"] == "freight":
            assert row["delta"] == -13
    filtered = compare_snapshots([tmp_path / (m["dataset_id"] + ".duckdb") for m in manifests],
        [plan_for(m, item_where={"field": "price", "operator": "gt", "values": [0]}) for m in manifests], tmp_path / "filtered")
    sales = next(r for r in filtered["metric_comparisons"] if r["metric"] == "sales")
    assert sales["delta"] == -145 and sales["reconciled"]
    assert not sales["membership"]["attribution_complete"]


def test_sql_ratio_projection_and_custom_expressions(snapshots):
    from app.sql_workspace import compile_exploration
    from app.query_worker import run_query
    directory, manifests = snapshots
    executor = Executor("formal", directory)
    plan, result = direct(directory, manifests["formal"], intent="compare", metric_ids=["sales"],
                          comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    session = Session("formal", last_result=result, last_successful_plan=plan, analysis_evidence={"record_id": "test"})
    direct_sql = "SELECT current_value,previous_value,absolute_change,change_rate FROM analysis_result"
    compiled, _ = compile_exploration(session, executor, direct_sql, [], "analysis_result")
    assert run_query(executor.path, compiled).rows == result.rows
    custom = "WITH x AS (SELECT current_value,previous_value,absolute_change,0.5 AS change_rate FROM analysis_result) SELECT * FROM x"
    compiled, _ = compile_exploration(session, executor, custom, [], "analysis_result")
    assert not compiled.ratios
    assert run_query(executor.path, compiled).rows[0][3] == Decimal("0.5")
    assert QueryResult(query_id="text", status="ok", columns=["sales"], rows=[["unconfirmed"]]).rows == [["unconfirmed"]]


def test_undefined_primary_metric_cannot_bind(snapshots):
    from app.workflow import bind_step
    directory, manifests = snapshots
    plan, result = direct(directory, manifests["formal"], intent="rank", dimension="category", metric_ids=["avg_review"])
    next_step = plan_for(manifests["formal"], source_step=1, bind_field="category_id")
    step, status, _ = bind_step(next_step, [{"status": "completed", "evidence": {"plan": plan.model_dump(), "result": result.model_dump()}}])
    assert step is None and status == "needs_input"
