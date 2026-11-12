import json
from copy import deepcopy
from decimal import Decimal

import pytest

from app.demo import practice_plans
from app.drilldown import drill_plan, selected_row_index
from app.models import EntityFilter, QueryResult, TimeRange
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.presentation import present
from app.session import Session
from app.session_store import SessionStore
from app.source import read_source
from app.sql_guard import validate_sql
from app.sql_workspace import SQLWorkspace, compile_exploration


class ScriptedProvider:
    model = "p1-scripted"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        value = next(self.replies)
        return value if isinstance(value, str) else value.model_dump_json()


def make_agent(executor, tmp_path, replies):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    provider = ScriptedProvider(replies)
    agent = AnalysisAgent(provider, {executor.manifest["dataset_id"]: executor}, store)
    return agent, store, Session(executor.manifest["dataset_id"])


def test_restore_full_conversation_pending_slots_and_result(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"]
    pending = plan.model_copy(update={"intent": "clarify", "time_range": None, "clarification": "请补充年份",
        "pending_intent": "aggregate", "missing_slots": ["year"], "month": 4})
    agent, store, session = make_agent(executor, tmp_path, [plan, *([practice_plans(executor.manifest)["Q08"]] * 5), pending])
    agent.respond(session, "全期销售额")
    for i in range(5):
        agent.respond(session, f"第{i}次解释关联")
    agent.respond(session, "4月销售额")
    restored = SessionStore(store.path).load(session.session_id, executor.manifest)
    assert len(restored.transcript) == 14 and len(restored.history) == 8
    assert restored.last_successful_plan == plan and restored.pending_plan == pending
    assert restored.last_result.query_id == session.last_result.query_id
    assert restored.registered_plans == {} and restored.last_evidence["type"] == "clarify"
    completed = agent.respond(restored, "2025年")
    assert completed.evidence["planning_calls"] == 0
    assert agent.provider.calls == 7 and restored.pending_plan is None
    assert len(store.list(session.dataset_id)) == 1


def test_restore_rejects_wrong_dataset_or_changed_sources(executor, tmp_path):
    agent, store, session = make_agent(executor, tmp_path, [practice_plans(executor.manifest)["Q02"]])
    agent.respond(session, "全期销售额")
    for field, value in [("dataset_id", "another"), ("metadata_sha256", "changed"), ("sources", [])]:
        manifest = deepcopy(executor.manifest)
        manifest[field] = value
        with pytest.raises(ValueError, match="绑定"):
            store.load(session.session_id, manifest)


def test_drill_nested_and_back_survives_restart_and_matches_oracle(executor, tmp_path):
    parent = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month"})
    agent, store, session = make_agent(executor, tmp_path, [parent])
    first = agent.respond(session, "按月汇总全期销售额")
    category = agent.drill(session, 0, "auto")
    check_rows(session.last_result, SourceOracle(session.dataset_id).evaluate(session.last_successful_plan))
    assert category.evidence["provenance"]["parent_record_id"] == first.evidence["record_id"]
    assert category.evidence["planning_calls"] == 0
    seller = agent.drill(session, 0, "auto")
    check_rows(session.last_result, SourceOracle(session.dataset_id).evaluate(session.last_successful_plan))
    assert session.last_successful_plan.filters[0].field == "category_id"
    restored = SessionStore(store.path).load(session.session_id, executor.manifest)
    assert len(restored.drill_stack) == 2
    assert agent.back(restored).evidence["record_id"] == category.evidence["record_id"]
    assert agent.back(restored).evidence["record_id"] == first.evidence["record_id"]
    assert restored.last_successful_plan == parent and not restored.drill_stack
    assert agent.provider.calls == 1 and seller.evidence["type"] == "query"


def test_drill_preserves_period_intersection_and_unrelated_filters(executor):
    parent = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month",
        "time_range": TimeRange(start="2025-04-15", end="2025-05-10"),
        "filters": [EntityFilter(field="customer_state", values=executor.manifest["entities"]["customer_states"][:1])]})
    result = QueryResult(query_id="test", status="ok", columns=["entity_id", "entity_name", "sales"], rows=[["2025-04", "2025-04", 1]])
    child = drill_plan(parent, result, 0, "category", executor.manifest)
    assert child.time_range == TimeRange(start="2025-04-15", end="2025-05-01")
    assert child.filters == parent.filters


def test_drill_rejects_payment_allocation_and_invalid_row(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month", "metric_ids": ["payment_amount"]})
    agent, _, session = make_agent(executor, tmp_path, [plan])
    agent.respond(session, "按月支付额")
    before = session.last_successful_plan
    with pytest.raises(ValueError, match="分摊"):
        agent.drill(session, 0, "category")
    with pytest.raises(ValueError, match="所选行"):
        agent.drill(session, 999, "month")
    assert session.last_successful_plan == before and session.drill_stack == []
    assert drill_plan(plan, session.last_result, 0, "auto", executor.manifest).dimension == "total"


def test_sql_over_full_audited_result_window_and_parameters(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month"})
    agent, store, session = make_agent(executor, tmp_path, [plan])
    agent.respond(session, "按月汇总销售额")
    # 模拟界面只留一行预览，SQL仍须从全量已审核计划计算。
    session.last_result.rows = session.last_result.rows[:1]
    session.last_result.truncated = True
    sql = """WITH shares AS (SELECT entity_id, sales, sales / SUM(sales) OVER () AS share FROM analysis_result)
             SELECT * FROM shares WHERE sales > ? ORDER BY entity_id"""
    evidence = SQLWorkspace(agent.executors, store).execute(session, sql, [0], "analysis_result")
    assert evidence["type"] == "sql_exploration" and evidence["business_audit"] == "custom_exploration"
    assert len(evidence["result"]["rows"]) > 1
    assert sum(Decimal(str(row[2])) for row in evidence["result"]["rows"]) == pytest.approx(Decimal(1))
    assert evidence["source_record_id"] == session.analysis_evidence["record_id"]
    assert session.last_successful_plan == plan
    assert present(evidence).csv_file and present(evidence).chart
    restored = store.load(session.session_id, executor.manifest)
    assert restored.last_evidence == evidence and restored.last_successful_plan == plan


def test_raw_sql_matches_independent_source_and_keeps_canonical_state(executor, tmp_path):
    agent, store, session = make_agent(executor, tmp_path, [])
    evidence = SQLWorkspace(agent.executors, store).execute(session,
        "SELECT order_status, COUNT(*) AS n FROM orders GROUP BY order_status ORDER BY order_status", [], "raw")
    counts = {}
    for row in read_source(session.dataset_id)["orders"]:
        counts[row["order_status"]] = counts.get(row["order_status"], 0) + 1
    assert evidence["result"]["rows"] == [[key, counts[key]] for key in sorted(counts)]
    assert session.last_successful_plan is None and session.last_result is None and not session.drill_stack


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_csv_auto('secret.csv')", "SELECT * FROM read_parquet('https://example.test/a')",
    "SELECT * FROM other.orders", "SELECT * FROM duckdb_tables()", "SELECT query('DELETE FROM orders')",
    "COPY orders TO 'output.csv'", "DELETE FROM orders", "SELECT 1; SELECT 2", "INSTALL httpfs",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x", "SELECT missing FROM orders",
    "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
])
def test_exploration_rejects_external_write_and_invalid_scope(sql):
    with pytest.raises(ValueError):
        validate_sql(sql, exploration=True)


def test_analysis_result_cannot_read_raw_or_shadow_audited_source(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"]
    agent, _, session = make_agent(executor, tmp_path, [plan])
    agent.respond(session, "全期销售额")
    for sql in ["SELECT * FROM orders", "WITH analysis_result AS (SELECT 99 AS sales) SELECT * FROM analysis_result"]:
        with pytest.raises(ValueError):
            compile_exploration(session, executor, sql, [], "analysis_result")


def test_sql_draft_does_not_execute_or_change_session(executor, tmp_path):
    agent, _, session = make_agent(executor, tmp_path, [])
    provider = ScriptedProvider([json.dumps({"sql": "SELECT order_status, COUNT(*) AS n FROM orders GROUP BY order_status", "explanation": "按原始状态分组"})])
    draft = SQLWorkspace(agent.executors).draft(session, "按订单状态统计数量", "raw", provider)
    assert draft.sql and provider.calls == 1
    assert session.last_evidence == {} and session.transcript == []


def test_table_sorting_does_not_change_selected_entity():
    evidence = {"type": "query", "result": {"columns": ["entity_id", "sales"], "rows": [["2025-01", 10], ["2025-06", 20]]}}
    assert selected_row_index(evidence, ["2025-06", "20.00"]) == 1


def test_saved_sql_draft_rejects_a_changed_analysis_source(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"]
    agent, store, session = make_agent(executor, tmp_path, [plan, plan.model_copy(update={"dimension": "month"})])
    agent.respond(session, "全期销售额")
    workspace = SQLWorkspace(agent.executors, store)
    draft = workspace.draft(session, "查看销售额", "analysis_result",
                            ScriptedProvider([json.dumps({"sql": "SELECT sales FROM analysis_result", "explanation": "查看销售额"})]))
    restored = store.load(session.session_id, executor.manifest)
    assert restored.sql_state["sql"] == draft.sql and restored.sql_state["scope"] == "analysis_result"
    agent.respond(restored, "按月看")
    with pytest.raises(ValueError, match="另一份分析结果"):
        workspace.execute(restored, draft.sql, [], "analysis_result")
    assert restored.last_evidence["type"] == "query"


def test_sql_bound_values_and_execution_errors_are_not_substituted(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"]
    agent, store, session = make_agent(executor, tmp_path, [plan])
    agent.respond(session, "全期销售额")
    workspace = SQLWorkspace(agent.executors, store)
    injection = workspace.execute(session, "SELECT COUNT(*) AS n FROM orders WHERE order_id = ?", ["x' OR 1=1 --"], "raw")
    assert injection["result"]["rows"] == [[0]]
    error = workspace.execute(session, "SELECT CAST('not-an-integer' AS INTEGER) AS n", [], "raw")
    assert error["result"]["status"] == "error" and error["result"]["rows"] == []
    assert present(error).table == [] and session.last_successful_plan == plan
    assert store.load(session.session_id, executor.manifest).last_evidence["result"]["status"] == "error"


def test_free_sql_month_function_is_validated_and_matches_source(executor, tmp_path):
    agent, _, session = make_agent(executor, tmp_path, [])
    evidence = SQLWorkspace(agent.executors).execute(session, """
        SELECT strftime(date_trunc('month', o.purchase_at), '%Y-%m') AS entity_id, SUM(i.price) AS sales
        FROM orders o JOIN order_items i ON i.order_id=o.order_id
        WHERE o.order_status='delivered' GROUP BY entity_id ORDER BY entity_id
    """, [], "raw")
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month"})
    expected = SourceOracle(session.dataset_id).evaluate(plan)
    check_rows(QueryResult.model_validate(evidence["result"]), [{key: row[key] for key in ("entity_id", "sales")} for row in expected])
