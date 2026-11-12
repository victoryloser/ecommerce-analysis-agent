import json
from copy import deepcopy

import httpx
import pytest

from app.clarification import bind_entities, choices, resolve_slots
from app.demo import practice_plans
from app.models import EntityFilter
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.semantic import catalog, get_catalog, validate_plan
from app.session import Session
from app.sql_audit import audit_query
from app.sql_compiler import CompiledQuery, compile_query
from app.sql_guard import validate_sql


class ScriptedProvider:
    model = "p0-test-scripted"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return next(self.replies).model_dump_json()


def test_selected_metadata_contains_dependencies_not_unrelated_tables(executor):
    plan = practice_plans(executor.manifest)["Q06"]
    summary = catalog(executor.manifest)
    detail = get_catalog(executor.manifest, plan)
    assert "expression" not in summary["metrics"]["avg_review"]
    assert detail["metrics"]["avg_review"]["dependencies"] == ["order_reviews.review_score"]
    assert "order_reviews" in detail["tables"] and "order_payments" not in detail["tables"]
    assert detail["tables"]["customers"]["fields"]["customer_unique_id"]["meaning"]


def test_catalog_request_returns_to_full_validation_and_query(executor):
    plan = practice_plans(executor.manifest)["Q06"]
    provider = ScriptedProvider([plan.model_copy(update={"intent": "load_catalog"}), plan])
    session = Session(plan.dataset_id)
    turn = AnalysisAgent(provider, {plan.dataset_id: executor}).respond(session, "全期按履约状态统计平均评分")
    assert turn.evidence["result"]["status"] == "ok"
    assert turn.evidence["planning_calls"] == 2 and turn.evidence["calls_used"] == 4
    events = [json.loads(line) for line in (executor.path.parent.parent / "runs" / turn.evidence["event_file"]).read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "catalog_loaded" and "order_reviews" in e["tables"] for e in events)


def test_explicit_year_fills_only_pending_slots_without_model(executor):
    source = practice_plans(executor.manifest)["Q03"]
    pending = source.model_copy(update={"intent": "clarify", "pending_intent": "compare", "time_range": None,
        "comparison_range": None, "month": 4, "comparison_month": 3, "missing_slots": ["year"],
        "clarification": "请补充年份", "filters": [EntityFilter(field="category_id", values=["electronics"])]})
    provider = ScriptedProvider([pending])
    agent = AnalysisAgent(provider, {source.dataset_id: executor})
    session = Session(source.dataset_id)
    agent.respond(session, "电子家电4月比3月的销售额变化？", False)
    turn = agent.respond(session, "2025年", False)
    assert provider.calls == 1 and turn.evidence["planning_calls"] == 0
    assert session.pending_plan is None and session.last_successful_plan.filters == pending.filters
    assert str(session.last_successful_plan.time_range.start) == "2025-04-01"
    assert choices(pending, executor.manifest)


def test_year_completion_crosses_calendar_year(executor):
    pending = practice_plans(executor.manifest)["Q03"].model_copy(update={"intent": "clarify", "pending_intent": "compare",
        "missing_slots": ["year"], "month": 1, "comparison_month": 12})
    resolved = resolve_slots(pending, "2025")
    assert str(resolved.time_range.start) == "2025-01-01"
    assert str(resolved.comparison_range.start) == "2024-12-01"
    assert resolve_slots(pending, "改查2025年订单数") is None


def test_duplicate_entity_name_requires_id_even_if_model_guesses(executor):
    manifest = deepcopy(executor.manifest)
    first, second = [r[0] for r in manifest["entities"]["sellers"][:2]]
    manifest["entities"]["sellers"][:2] = [[first, "同名商家"], [second, "同名商家"]]
    plan = practice_plans(manifest)["Q02"].model_copy(update={"filters": [EntityFilter(field="seller_id", values=[first])]})
    pending = bind_entities(plan, manifest, "同名商家的商品销售额")
    assert pending.intent == "clarify" and len(pending.entity_candidates) == 2
    completed = resolve_slots(pending, second)
    completed = bind_entities(completed, manifest, second)
    validate_plan(completed, manifest)
    assert completed.filters[0].values == [second] and completed.time_range == plan.time_range


def test_full_round_budget_stops_repeated_catalog_requests(executor):
    request = practice_plans(executor.manifest)["Q01"].model_copy(update={"intent": "load_catalog"})
    provider = ScriptedProvider([request] * 5)
    session = Session(request.dataset_id)
    with pytest.raises(ValueError, match="8次"):
        AnalysisAgent(provider, {session.dataset_id: executor}).respond(session, "全期订单数")
    assert provider.calls == 4 and session.last_successful_plan is None


def test_http_failure_is_logged_and_reraised_without_credentials(executor):
    secret = "not-for-logs-test-credential"
    class FailingProvider:
        model = "failure-test"
        def complete(self, messages):
            req = httpx.Request("POST", f"https://example.test/?key={secret}", headers={"Authorization": secret})
            response = httpx.Response(401, request=req, text=secret)
            raise httpx.HTTPStatusError(secret, request=req, response=response)
    session = Session(executor.manifest["dataset_id"])
    with pytest.raises(httpx.HTTPStatusError):
        AnalysisAgent(FailingProvider(), {session.dataset_id: executor}).respond(session, "全期订单数")
    from app.config import RUNS_DIR
    text = (RUNS_DIR / session.dataset_id / "events" / f"{session.session_id}.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    assert secret not in text and events[-1]["event"] == "turn_failed"
    assert events[-1]["http_status"] == 401
    assert not any(e["event"] == "query_started" for e in events)


@pytest.mark.parametrize("qid,old,new", [
    ("Q01", "COUNT(DISTINCT order_id)", "COUNT(order_id)"),
    ("Q01", "o.order_status='delivered'", "o.order_status='canceled'"),
    ("Q02", "o.purchase_at", "o.delivered_at"),
    ("Q05", "rank_no<=1", "rank_no<=10"),
    ("Q05", "RANK()", "ROW_NUMBER()"),
])
def test_business_audit_rejects_compiler_mutations(executor, qid, old, new):
    plan = practice_plans(executor.manifest)[qid]
    query = compile_query(plan)
    changed = query.sql.replace(old, new)
    assert changed != query.sql
    with pytest.raises(ValueError, match="口径审计"):
        audit_query(plan, CompiledQuery(changed, query.parameters))


@pytest.mark.parametrize("sql", ["SELECT missing_column FROM orders", "WITH x AS (SELECT order_id FROM orders) SELECT price FROM x", "SELECT c.order_id FROM orders o"])
def test_column_scope_is_checked(sql):
    with pytest.raises(ValueError, match="列或关联作用域"):
        validate_sql(sql)


def test_real_join_example_matches_source_counts_and_amounts(executor):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"intent": "join_example", "metric_ids": []})
    session = Session(plan.dataset_id)
    result, _ = executor.execute_plan(session, executor.register(session, plan))
    expected = SourceOracle(plan.dataset_id).evaluate(plan)
    assert expected
    check_rows(result, expected)
