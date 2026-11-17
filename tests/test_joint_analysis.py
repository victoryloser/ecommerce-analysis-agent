"""本地构造的联合维度验收，覆盖真实组合、质量、排名和版本关系。"""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import duckdb
import pytest

from test_semifinal_quality import tables, build
from app.models import AnalysisPlan, QueryResult, PlanUpdate, Predicate
from app.sql_compiler import compile_query
from app.quality import decode_quality
from app.semantic import validate_plan, PlanError
from app.oracle import SourceOracle, check_rows
from app.drilldown import drill_plan, selected_row_index
from app.workflow import bind_step
from app.plan_update import apply_update
from app.snapshot_diff import compare_snapshots


@pytest.fixture
def joint_tables(tables):
    tables["sellers"].append({**tables["sellers"][0], "seller_id": "S2", "seller_name": "S2"})
    tables["order_items"][1].update(product_id="K2", seller_id="S2")
    tables["order_items"][3].update(seller_id="S2")
    item = tables["order_items"][2]
    tables["order_items"] += [{**item, "order_item_id": 2, "price": 50},
                              {**item, "order_item_id": 3, "product_id": "K2", "seller_id": "S2", "price": 70}]
    tables["orders"][2]["delivered_at"] = "2027-04-05"
    for oid, score in (("a1", 1), ("a2", 5)):
        from app.source import SCHEMA
        tables["order_reviews"].append({key: {"review_id": oid, "order_id": oid, "review_score": score}.get(key) for key in SCHEMA["order_reviews"]})
    return tables


def plan_for(manifest, **updates):
    return AnalysisPlan.model_validate({"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
        "intent": "aggregate", "metric_ids": ["sales", "order_count", "delay_rate", "avg_review", "low_score_rate"],
        "group_by": ["category", "seller"], "time_range": {"start": "2027-04-01", "end": "2027-05-01"}, **updates})


def execute(tmp_path, manifest, plan):
    from app.sql_audit import audit_query
    from app.sql_guard import validate_sql
    from app.quality import QUERY_SCHEMA
    validate_plan(plan, manifest)
    compiled = compile_query(plan)
    audit_query(plan, compiled)
    validate_sql(compiled.sql, schema=QUERY_SCHEMA, exploration=True)
    with duckdb.connect(str(tmp_path / f"{manifest['dataset_id']}.duckdb"), read_only=True) as conn:
        cursor = conn.execute(compiled.sql, compiled.parameters)
        columns, rows, quality = decode_quality([c[0] for c in cursor.description], cursor.fetchall())
    return QueryResult(query_id="joint-test", status="ok" if rows else "empty", columns=columns, rows=rows, quality=quality)


def records(result):
    return [dict(zip(result.columns, row)) for row in result.rows]


def evidence(manifest, plan, result):
    return {"type": "query", "dataset_id": manifest["dataset_id"], "record_id": result.query_id,
            "plan": plan.model_dump(mode="json"), "result": result.model_dump()}


def test_joint_cells_and_order_grain(tmp_path, joint_tables):
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest)
    result = execute(tmp_path, manifest, plan)
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))
    rows = records(result)
    assert {(r["category_id"], r["seller_id"]): (r["sales"], r["order_count"], r["avg_review"]) for r in rows} == {
        ("K1", "S1"): (150, 1, 1), ("K1", "S2"): (100, 1, 5), ("K2", "S2"): (70, 1, 1)}
    total = execute(tmp_path, manifest, plan.model_copy(update={"group_by": None}))
    assert records(total)[0]["sales"] == sum(r["sales"] for r in rows)
    assert records(total)[0]["order_count"] == 2 < sum(r["order_count"] for r in rows)
    assert records(total)[0]["avg_review"] == 3


@pytest.mark.parametrize("group_by", [["category", "seller"], ["month", "category"]])
def test_conflict_and_scope_quality_matches_independent_oracle(tmp_path, joint_tables, group_by):
    joint_tables["order_items"].append({**joint_tables["order_items"][-1], "price": 110})
    manifest = build(tmp_path, joint_tables)
    oracle = SourceOracle("fixture", joint_tables)
    plan = plan_for(manifest, group_by=group_by)
    result = execute(tmp_path, manifest, plan)
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    assert next(r for r in records(result) if r["category_id"] == "K2")["sales"] is None
    plan = plan.model_copy(update={"order_where": Predicate(field="order_sales", operator="gt", values=[240])})
    result = execute(tmp_path, manifest, plan)
    check_rows(result, oracle.evaluate(plan, with_quality=True))
    assert all(r["sales"] is None and r["order_count"] is None for r in records(result))
    assert all(q["order_count"]["membership_unknown_count"] == 1 for q in result.quality)


def test_joint_ranking_partitions_and_ties(tmp_path, joint_tables):
    joint_tables["order_items"][3]["price"] = 150
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="rank", metric_ids=["sales"], rank_within="category", top_n=1)
    result = execute(tmp_path, manifest, plan)
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))
    assert len(result.rows) == 3
    assert all(r["rank_no"] == 1 for r in records(result))


def test_unknown_rank_only_blocks_its_partition_and_impossible_candidate_is_excluded(tmp_path, joint_tables):
    joint_tables["order_items"].append({**joint_tables["order_items"][-1], "price": 110})
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="rank", metric_ids=["sales"], rank_within="category", top_n=1)
    result = execute(tmp_path, manifest, plan)
    assert [(r["category_id"], r["rank_no"]) for r in records(result)] == [("K1", 1), ("K2", None)]
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))
    plan = plan.model_copy(update={"min_group_count": 2, "order_where": Predicate(field="order_sales", operator="gt", values=[240])})
    assert execute(tmp_path, manifest, plan).rows == []


def test_joint_comparison_parent_contribution_and_full_denominator(tmp_path, joint_tables):
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="contribution", metric_ids=["sales"], contribution_within="category", top_n=1,
                    comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    result = execute(tmp_path, manifest, plan)
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))
    rows = records(result)
    assert len(rows) == 2
    assert all(r["total_change"] == 120 for r in rows)
    k1 = next(r for r in rows if r["category_id"] == "K1")
    assert k1["sales_change"] == 100 and k1["parent_change"] == 150
    plan = plan.model_copy(update={"contribution_direction": "decrease", "contribution_within": None})
    result = execute(tmp_path, manifest, plan)
    assert records(result)[0]["sales_change"] == -30
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))


def test_joint_drill_preserves_both_members_and_month(tmp_path, joint_tables):
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest)
    result = execute(tmp_path, manifest, plan)
    child = drill_plan(plan, result, 0, "items", manifest)
    assert {f.field: f.values for f in child.filters} == {"category_id": ["K1"], "seller_id": ["S1"]}
    child_result = execute(tmp_path, manifest, child)
    assert sum(r["price"] for r in records(child_result)) == 150
    assert selected_row_index(evidence(manifest, plan, result), result.rows[1]) == 1
    monthly = plan_for(manifest, group_by=["month", "category"], time_range={"start": "2027-03-01", "end": "2027-05-01"})
    result = execute(tmp_path, manifest, monthly)
    child = drill_plan(monthly, result, 0, "orders", manifest)
    assert str(child.time_range.end) == "2027-04-01"
    assert child.group_by is None


def test_group_binding_partition_selection_and_partial_override(tmp_path, joint_tables):
    manifest = build(tmp_path, joint_tables)
    parent = plan_for(manifest, intent="rank", metric_ids=["sales"], rank_within="category", top_n=1)
    result = execute(tmp_path, manifest, parent)
    step = plan_for(manifest, intent="details", metric_ids=[], group_by=None, source_step=1, bind_mode="group")
    tasks = [{"status": "completed", "evidence": evidence(manifest, parent, result)}, {"status": "pending", "evidence": {}}]
    assert bind_step(step, tasks)[1] == "needs_input"
    step = step.model_copy(update={"source_partition": "K1"})
    bound, _, _ = bind_step(step, tasks)
    assert {f.field: f.values for f in bound.filters} == {"category_id": ["K1"], "seller_id": ["S1"]}
    tasks[1] = {"status": "completed", "evidence": {"plan": bound.model_dump(mode="json")}}
    workflow = plan_for(manifest, intent="workflow", metric_ids=[], group_by=None, steps=[parent, step])
    changed = apply_update(workflow, PlanUpdate(operation="set", field="seller_id", values=["S2"], target_step=2), manifest, tasks)
    chosen = changed.steps[1]
    assert chosen.source_step is None and chosen.bind_mode == "field"
    assert next(f.values for f in chosen.filters if f.field == "category_id") == ["K1"]
    assert chosen.item_where.field == "seller_id" and chosen.item_where.values == ["S2"]


def test_group_binding_and_drill_keep_selected_order(tmp_path, joint_tables):
    joint_tables["order_items"][3]["seller_id"] = "S1"
    manifest = build(tmp_path, joint_tables)
    parent = plan_for(manifest, selected_order_id="a1")
    result = execute(tmp_path, manifest, parent)
    step = plan_for(manifest, group_by=None, source_step=1, bind_mode="group")
    bound, _, _ = bind_step(step, [{"status": "completed", "evidence": evidence(manifest, parent, result)}])
    assert bound.selected_order_id == "a1"
    assert records(execute(tmp_path, manifest, bound))[0]["sales"] == 150
    child = drill_plan(parent, result, 0, "items", manifest)
    assert child.selected_order_id == "a1"
    assert {r["order_id"] for r in records(execute(tmp_path, manifest, child))} == {"a1"}


def test_unknown_partition_rank_has_no_total_conclusion(tmp_path, joint_tables):
    from app.answers import conclusion
    for item in joint_tables["order_items"]:
        item["price"] = None
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="rank", metric_ids=["sales"], rank_within="category")
    result = execute(tmp_path, manifest, plan)
    assert "各父分组独立排名" in conclusion(plan, result)


@pytest.mark.parametrize("dims", [["category", "seller"], ["month", "category"]])
def test_joint_snapshot_members_and_review_samples(tmp_path, joint_tables, dims):
    old = build(tmp_path, joint_tables, "old")
    revised = deepcopy(joint_tables)
    revised["order_items"][2]["price"] = 120
    new = build(tmp_path, revised, "new")
    plans = [plan_for(m, group_by=dims) for m in (old, new)]
    report = compare_snapshots([tmp_path/"old.duckdb", tmp_path/"new.duckdb"], plans, tmp_path/"diff")
    changes = [r for r in report["metric_comparisons"] if r["metric"] == "sales" and r["delta"]]
    assert len(changes) == 1 and changes[0]["delta"] == 20 and changes[0]["reconciled"]
    assert json.loads(changes[0]["entity_id"])["category"] == "K1"
    for row in report["metric_comparisons"]:
        if row["metric"] == "avg_review":
            assert row["samples_before"]["reviewed_orders"] <= 2


def test_contract_compatibility_and_restrictions(tmp_path, joint_tables):
    manifest = build(tmp_path, joint_tables)
    assert plan_for(manifest, group_by=["seller", "category"]).group_by == ["category", "seller"]
    assert plan_for(manifest, group_by=["category"]).dimension == "category"
    for updates in ({"group_by": [], "dimension": "seller"}, {"group_by": ["seller", "seller"]}):
        with pytest.raises(ValueError):
            plan_for(manifest, **updates)
    for updates in ({"metric_ids": ["payment_amount"]}, {"metric_ids": ["repeat_customers"]},
                    {"group_by": ["month", "category"], "intent": "compare", "comparison_range": {"start": "2027-03-01", "end": "2027-04-01"}}):
        with pytest.raises(PlanError):
            validate_plan(plan_for(manifest, **updates), manifest)
    plan = apply_update(plan_for(manifest, intent="rank", rank_within="category"), PlanUpdate(operation="set", field="group_by", values=["category"]), manifest)
    assert plan.dimension == "category" and plan.group_by is None and plan.rank_within is None


def test_joint_presentation_keeps_month_series_and_click_keys(tmp_path, joint_tables, monkeypatch):
    from app.presentation import present
    from app.charts import render_charts, numeric_columns
    manifest = build(tmp_path, joint_tables)
    monkeypatch.setattr("app.presentation.RUNS_DIR", tmp_path)
    (tmp_path/"fixture").mkdir()
    plan = plan_for(manifest, group_by=["month", "category"], metric_ids=["sales"],
                    time_range={"start": "2027-03-01", "end": "2027-05-01"})
    result = execute(tmp_path, manifest, plan)
    record = evidence(manifest, plan, result)
    view = present(record)
    assert view.chart_kind == "line" and view.chart_series == "品类"
    assert {p["分组"] for p in view.chart} == {"2027-03", "2027-04"}
    assert len({p["品类"] for p in view.chart}) == 2
    html = render_charts(record, ["sales"])
    from html import unescape
    assert all(r["entity_id"] in unescape(html) for r in records(result))
    assert numeric_columns(record) == ["sales"]
    assert "category_id" in Path(view.csv_file).read_text(encoding="utf-8-sig")


def test_parent_zero_and_unknown_do_not_replace_global_denominator(tmp_path, joint_tables):
    joint_tables["order_items"][0]["price"] = 250
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="contribution", metric_ids=["sales"], contribution_within="category",
                    comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    result = execute(tmp_path, manifest, plan)
    check_rows(result, SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True))
    for row in records(result):
        if row["category_id"] == "K1":
            assert row["parent_change"] == 0 and row["parent_contribution_rate"] is None
            assert row["contribution_rate"] is not None


def test_same_names_do_not_merge_or_break_encoded_keys(tmp_path, joint_tables):
    for row in joint_tables["categories"]:
        row["category_name_zh"] = "同名 / 品类"
    for row in joint_tables["sellers"]:
        row["seller_name"] = "同名商家"
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, metric_ids=["sales"])
    result = execute(tmp_path, manifest, plan)
    assert len({r["entity_name"] for r in records(result)}) == 1
    assert len({r["entity_id"] for r in records(result)}) == 3
    child = drill_plan(plan, result, 1, "items", manifest)
    assert {f.field: f.values for f in child.filters} == {"category_id": ["K1"], "seller_id": ["S2"]}


def test_quality_and_period_assertions_reject_wrong_evidence(tmp_path, joint_tables):
    from app.evaluation_cases import assert_plan
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest)
    result = execute(tmp_path, manifest, plan)
    expected = SourceOracle("fixture", joint_tables).evaluate(plan, with_quality=True)
    result.quality[0]["sales"]["known_value"] = 999
    with pytest.raises(AssertionError, match="已确认值"):
        check_rows(result, expected)
    with pytest.raises(AssertionError, match="period_mode"):
        assert_plan(plan.model_copy(update={"period_mode": "full_coverage"}), plan)


def test_workflow_restoration_and_snapshot_group_reselection(tmp_path, joint_tables, monkeypatch):
    from app.executor import Executor
    from app.orchestrator import AnalysisAgent
    from app.session import Session
    from app.session_store import SessionStore
    from app.evaluate import check_workflow_evidence
    from test_semifinal_state import Scripted
    for module in ("app.evidence", "app.trace", "app.snapshot_comparison"):
        monkeypatch.setattr(module+".RUNS_DIR", tmp_path/"runs")
    old = build(tmp_path, joint_tables, "old")
    revised = deepcopy(joint_tables)
    revised["order_items"][3]["price"] = 300
    build(tmp_path, revised, "new")
    parent = plan_for(old, intent="rank", metric_ids=["sales"], top_n=1)
    follow = plan_for(old, group_by=None, metric_ids=["sales"], source_step=1, bind_mode="group")
    workflow = plan_for(old, intent="workflow", group_by=None, metric_ids=[], steps=[parent, follow])
    store = SessionStore(tmp_path/"sessions.sqlite3")
    agent = AnalysisAgent(Scripted(workflow), {name: Executor(name, tmp_path) for name in ("old", "new")}, store)
    session = Session("old")
    turn = agent.respond(session, "组合冠军，再看该组合商品额")
    check_workflow_evidence(turn.evidence, workflow, SourceOracle("old", joint_tables))
    restored = store.load(session.session_id, agent.executors["old"].manifest)
    assert restored.working_plan == workflow
    assert all(t["status"] == "completed" for t in restored.workflow_tasks)
    restored.focus_step = 2
    state = agent.compare(restored, {"target_dataset_id": "new", "source": "working_plan", "mode": "intent_replay"}).evidence
    assert state["status"] == "completed"
    scopes = [{f["field"]: f["values"] for f in state["stages"][side]["effective_plan"]["filters"]} for side in ("source", "target")]
    assert scopes == [{"category_id": ["K1"], "seller_id": ["S1"]}, {"category_id": ["K1"], "seller_id": ["S2"]}]
    assert all(r["delta"] is None for r in state["stages"]["diff"]["report"]["metric_comparisons"])
    restored.analysis_evidence = restored.workflow_tasks[1]["evidence"]
    fixed = agent.compare(restored, {"target_dataset_id": "new", "source": "current_result", "mode": "fixed_scope"}).evidence
    assert fixed["status"] == "completed"
    assert fixed["stages"]["target"]["effective_plan"]["filters"] == restored.analysis_evidence["plan"]["filters"]


def test_p1_joint_four_cells_d2_and_top_n_do_not_truncate_candidates(tmp_path, joint_tables):
    old = build(tmp_path, joint_tables, "old")
    revised = deepcopy(joint_tables)
    revised["order_payments"][2]["payment_value"] = 40
    revised["order_payments"].append({**revised["order_payments"][2], "payment_sequence": 2, "payment_value": 60})
    revised = {k: list(reversed(v)) for k, v in revised.items()}
    new = build(tmp_path, revised, "new")
    plans = [plan_for(m, intent="contribution", metric_ids=["sales"], top_n=1,
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"}) for m in (old, new)]
    report = compare_snapshots([tmp_path/"old.duckdb", tmp_path/"new.duckdb"], plans, tmp_path/"diff")
    assert report["business_diff"]["d2_equivalent"]
    assert len(report["reconciliation"]) == 3
    assert all(r["verified"] for r in report["reconciliation"])
    assert len({r["entity_id"] for r in report["metric_comparisons"]}) == 3
    assert sum(r["displayed_before"] for r in report["metric_comparisons"] if r["period"] == "current") == 1


def test_contribution_answer_is_not_a_page_based_global_champion(tmp_path, joint_tables):
    from app.answers import answer
    manifest = build(tmp_path, joint_tables)
    plan = plan_for(manifest, intent="contribution", metric_ids=["sales"],
                    comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    result = execute(tmp_path, manifest, plan)
    result = result.model_copy(update={"rows": result.rows[1:], "quality": result.quality[1:], "row_offset": 1})
    text = answer(plan, result, manifest)
    assert "贡献最大的分组" not in text and "分母使用全部" in text


def test_joint_large_result_keeps_full_denominator_and_snapshot_evidence(tmp_path, tables):
    from app.executor import Executor
    from app.session import Session
    import csv
    tables["sellers"] = [{**tables["sellers"][0], "seller_id": f"S{i:04}", "seller_name": "同名"} for i in range(501)]
    template = tables["order_items"][0]
    tables["order_items"] = [{**template, "order_id": oid, "order_item_id": i+1,
        "seller_id": f"S{i:04}", "price": 1 if oid == "m1" else i+2}
        for oid in ("m1", "a1") for i in range(501)]
    old = build(tmp_path, tables, "old")
    revised = deepcopy(tables)
    revised["order_items"][-1]["price"] += 10
    new = build(tmp_path, revised, "new")
    plan = plan_for(old, metric_ids=["sales"])
    executor, session = Executor("old", tmp_path), Session("old")
    page, _ = executor.execute_plan(session, executor.register(session, plan))
    assert page.truncated and len(page.rows) == 500
    tail, _ = executor.execute_plan(session, executor.register(session, plan), offset=500)
    assert len(tail.rows) == 1 and not tail.truncated
    contributions = [plan_for(m, intent="contribution", metric_ids=["sales"], top_n=1,
        comparison_range={"start": "2027-03-01", "end": "2027-04-01"}) for m in (old, new)]
    result = execute(tmp_path, old, contributions[0])
    assert len(result.rows) == 1 and records(result)[0]["total_change"] == sum(range(1, 502))
    report = compare_snapshots([tmp_path/"old.duckdb", tmp_path/"new.duckdb"], contributions, tmp_path/"diff")
    assert len(report["reconciliation"]) == 501
    assert all(r["verified"] for r in report["reconciliation"])
    current = [r for r in report["metric_comparisons"] if r["period"] == "current"]
    assert len(current) == 501 and sum(r["delta"] for r in current) == 10
    assert sum(r["displayed_before"] for r in current) == 1
    with open(report["csv_file"], encoding="utf-8-sig", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == report["diff_rows"]
