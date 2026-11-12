"""跨意图、工作流和持久化的澄清回归；固定Provider不计作模型理解成绩。"""
from copy import deepcopy
from decimal import Decimal

import pytest

from app.clarification import bind_entities, choices, resolve_slots
from app.demo import practice_plans
from app.models import AnalysisPlan, EntityFilter, Predicate, TimeRange
from app.orchestrator import AnalysisAgent
from app.semantic import PlanError, validate_plan
from app.session import Session
from app.session_store import SessionStore
from app.source import read_source


class ScriptedProvider:
    model = "unified-clarification-scripted"

    def __init__(self, plans):
        self.plans = iter(plans)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return next(self.plans).model_dump_json()


def analysis(manifest, intent):
    plan = practice_plans(manifest)["Q02"].model_copy(update={
        "intent": intent, "time_range": TimeRange(start="2025-04-01", end="2025-05-01"),
        "metric_ids": ["sales"] if intent == "contribution" else []})
    if intent == "contribution":
        plan = plan.model_copy(update={"dimension": "category", "comparison_range": TimeRange(start="2025-03-01", end="2025-04-01")})
    return plan


def missing_year(plan):
    return plan.model_copy(update={"intent": "clarify", "pending_intent": plan.intent,
        "time_range": None, "comparison_range": None, "missing_slots": ["year"], "month": 4,
        "comparison_month": 3 if plan.intent == "contribution" else None, "clarification": "请补充年份"})


def duplicate_sellers(manifest):
    manifest = deepcopy(manifest)
    first, second = [r[0] for r in manifest["entities"]["sellers"][:2]]
    manifest["entities"]["sellers"][:2] = [[first, "同名商家"], [second, "同名商家"]]
    return manifest, first, second


@pytest.mark.parametrize("intent", ["contribution", "details", "diagnose"])
def test_new_intents_disambiguate_guessed_ids_and_preserve_scope(executor, intent):
    manifest, first, second = duplicate_sellers(executor.manifest)
    plan = analysis(manifest, intent).model_copy(update={
        "filters": [EntityFilter(field="seller_id", values=[first])], "time_field": "delivered_at",
        "order_statuses": ["delivered", "cancelled"], "detail_level": "payments",
        "diagnosis": "quality", "diagnostic_threshold": 0.05,
        "item_where": Predicate(field="price", operator="between", values=[50, 300])})
    pending = bind_entities(plan, manifest, "同名商家的分析")
    # 往返Schema，避免model_copy绕过pending_intent类型检查。
    pending = AnalysisPlan.model_validate_json(pending.model_dump_json())
    validate_plan(pending, manifest)
    assert pending.intent == "clarify" and pending.pending_intent == intent
    assert {value for _, value in choices(pending, manifest)} == {first, second}
    completed = bind_entities(resolve_slots(pending, second), manifest, second)
    validate_plan(completed, manifest)
    assert completed == plan.model_copy(update={"filters": [EntityFilter(field="seller_id", values=[second])]})


@pytest.mark.parametrize("intent,option", [
    ("details", "orders"), ("details", "items"), ("details", "payments"), ("details", "reviews"),
    ("diagnose", "reconciliation"), ("diagnose", "quality"), ("diagnose", "sales_spike"), ("diagnose", "seller_outlier"),
    ("contribution", None), ("join_example", None),
])
def test_year_completion_executes_all_new_variants_without_model(executor, intent, option):
    plan = analysis(executor.manifest, intent)
    if option:
        plan = plan.model_copy(update={"detail_level" if intent == "details" else "diagnosis": option})
    provider = ScriptedProvider([missing_year(plan)])
    session = Session(plan.dataset_id)
    agent = AnalysisAgent(provider, {session.dataset_id: executor})
    first = agent.respond(session, "4月的分析")
    assert first.evidence["type"] == "clarify" and not session.registered_plans
    turn = agent.respond(session, "2025年")
    assert provider.calls == 1 and turn.evidence["planning_calls"] == 0
    assert session.pending_plan is None and session.last_successful_plan == plan
    assert turn.evidence["result"]["status"] in {"ok", "empty"}
    assert turn.evidence["sql"] and turn.evidence["parameters"]


def test_contribution_year_completion_crosses_year_and_keeps_fixed_baseline(executor):
    pending = missing_year(analysis(executor.manifest, "contribution")).model_copy(update={"month": 1, "comparison_month": 12})
    completed = resolve_slots(pending, "2025")
    assert str(completed.time_range.start) == "2025-01-01"
    assert str(completed.comparison_range.start) == "2024-12-01"
    fixed = pending.model_copy(update={"comparison_month": None, "comparison_range": completed.comparison_range})
    validate_plan(fixed, executor.manifest)
    assert resolve_slots(fixed, "2025").comparison_range == completed.comparison_range
    with pytest.raises(PlanError, match="基期月份"):
        validate_plan(fixed.model_copy(update={"comparison_range": None}), executor.manifest)


def test_multiple_ambiguities_keep_other_explicit_ids_and_boolean_predicates(executor):
    manifest, first, second = duplicate_sellers(executor.manifest)
    cat1, cat2 = [r[0] for r in manifest["entities"]["categories"][:2]]
    manifest["entities"]["categories"][:2] = [[cat1, "同名品类"], [cat2, "同名品类"]]
    plan = analysis(manifest, "details").model_copy(update={
        "filters": [EntityFilter(field="seller_id", values=[first, "同名商家"])],
        "item_where": Predicate(conjunction="any", children=[
            Predicate(field="category_id", operator="in", values=["同名品类"]),
            Predicate(field="price", operator="between", values=[50, 300])], negate=True)})
    pending = bind_entities(plan, manifest, f"保留{first}，再包括同名商家，排除符合条件的同名品类或价格")
    assert pending.filters[0].values == [first, "同名商家"]
    second_pending = bind_entities(resolve_slots(pending, second), manifest, second)
    assert second_pending.intent == "clarify" and second_pending.missing_slots == ["category_id"]
    assert second_pending.filters[0].values == [first, second]
    completed = bind_entities(resolve_slots(second_pending, cat2), manifest, cat2)
    validate_plan(completed, manifest)
    assert completed.item_where.children[0].values == [cat2]
    assert completed.item_where.children[1] == plan.item_where.children[1]
    assert completed.item_where.negate and completed.item_where.conjunction == "any"


def test_new_intents_bind_unique_names_and_keep_literal_predicates(executor):
    manifest = executor.manifest
    key, name = manifest["entities"]["sellers"][0]
    for intent in ("details", "diagnose", "contribution"):
        plan = analysis(manifest, intent).model_copy(update={
            "filters": [EntityFilter(field="seller_id", values=[name])],
            "item_where": Predicate(field="seller_id", operator="not_in", values=["no-such-seller"])})
        bound = bind_entities(plan, manifest)
        assert bound.filters[0].values == [key]
        assert bound.item_where.values == ["no-such-seller"]
        validate_plan(bound, manifest)


def test_workflow_collects_slots_before_execution_and_restores_dependencies(executor, monkeypatch, tmp_path):
    manifest, first, second = duplicate_sellers(executor.manifest)
    monkeypatch.setattr(executor, "manifest", manifest)
    first_plan = analysis(manifest, "contribution").model_copy(update={"filters": [EntityFilter(field="seller_id", values=[first])]})
    details = analysis(manifest, "details").model_copy(update={"source_step": 1, "source_row": 0, "bind_field": "category_id"})
    diagnostic = analysis(manifest, "diagnose").model_copy(update={"diagnosis": "quality"})
    explanation = practice_plans(manifest)["Q08"]
    workflow = first_plan.model_copy(update={"intent": "workflow", "filters": [],
        "steps": [missing_year(first_plan), details, missing_year(diagnostic), explanation]})
    provider = ScriptedProvider([workflow])
    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = Session(workflow.dataset_id)
    agent = AnalysisAgent(provider, {session.dataset_id: executor}, store)
    pending = agent.respond(session, "同名商家4月比3月各品类增长贡献，查最高品类订单，再做4月质量诊断并解释关联")
    assert pending.evidence["type"] == "clarify" and "步骤1" in pending.text
    assert ("2025年", "2025年") in choices(session.pending_plan, manifest)
    assert not session.registered_plans and session.last_result is None

    session = store.load(session.session_id, manifest)
    year = agent.respond(session, "2025年")
    assert year.evidence["planning_calls"] == 0 and "步骤1" in year.text
    assert {v for _, v in choices(session.pending_plan, manifest)} == {first, second}
    entity = agent.respond(session, second)
    assert entity.evidence["planning_calls"] == 0 and "步骤3" in entity.text
    assert session.pending_plan.steps[1] == details
    assert not session.registered_plans and session.last_result is None

    session = store.load(session.session_id, manifest)
    turn = agent.respond(session, "2025年")
    assert provider.calls == 1 and turn.evidence["type"] == "workflow"
    assert turn.evidence["planning_calls"] == 0 and turn.evidence["calls_used"] == 3
    assert session.pending_plan is None
    assert turn.evidence["plan"]["steps"][1] == details.model_dump(mode="json")
    contribution, detail_record, diagnosis, explained = turn.evidence["steps"]
    assert contribution["result"]["status"] == detail_record["result"]["status"] == "ok"
    assert diagnosis["result"]["status"] == "ok" and explained["type"] == "explain"
    selected_category = contribution["result"]["rows"][0][0]
    assert detail_record["plan"]["filters"] == [{"field": "category_id", "values": [selected_category]}]
    assert contribution["plan"]["filters"] == [{"field": "seller_id", "values": [second]}]

    # 从原始材料独立计算两期销售净变化，核对补槽后实际执行的实体和期间。
    tables = read_source(session.dataset_id)
    orders = {r["order_id"]: r for r in tables["orders"]}
    expected = Decimal(0)
    for item in tables["order_items"]:
        order = orders[item["order_id"]]
        day = str(order["purchase_at"])[:10]
        if item["seller_id"] == second and order["order_status"] == "delivered":
            weight = 1 if "2025-04-01" <= day < "2025-05-01" else -1 if "2025-03-01" <= day < "2025-04-01" else 0
            expected += weight * Decimal(str(item["price"]))
    assert all(Decimal(row[contribution["result"]["columns"].index("total_change")]) == expected for row in contribution["result"]["rows"])
    assert store.load(session.session_id, manifest).last_evidence == turn.evidence


def test_workflow_rejects_bad_dependencies_even_while_waiting(executor):
    first = missing_year(analysis(executor.manifest, "details")).model_copy(update={"source_step": 1, "bind_field": "seller_id"})
    workflow = first.model_copy(update={"intent": "workflow", "source_step": None, "bind_field": None, "steps": [first]})
    pending = bind_entities(workflow, executor.manifest)
    with pytest.raises(PlanError, match="前面的步骤"):
        validate_plan(pending, executor.manifest)


def test_new_question_discards_pending_workflow_and_keeps_previous_success(executor, tmp_path):
    previous = practice_plans(executor.manifest)["Q02"]
    step = missing_year(analysis(executor.manifest, "details"))
    workflow = previous.model_copy(update={"intent": "workflow", "steps": [step]})
    history = previous.model_copy(update={"intent": "history", "history_query": "销售额"})
    provider = ScriptedProvider([previous, workflow, history])
    store = SessionStore(tmp_path / "sessions.sqlite3")
    agent = AnalysisAgent(provider, {previous.dataset_id: executor}, store)
    session = Session(previous.dataset_id)
    agent.respond(session, "全期销售额")
    last_result = session.last_result
    agent.respond(session, "先查4月订单")
    assert session.pending_plan.pending_intent == "workflow"
    assert resolve_slots(session.pending_plan, "改为查历史销售额") is None
    turn = agent.respond(session, "改为查历史销售额")
    assert turn.evidence["type"] == "history" and session.pending_plan is None
    assert session.last_successful_plan == previous and session.last_result == last_result


def test_year_choices_use_selected_date_field_coverage(executor):
    manifest = deepcopy(executor.manifest)
    manifest["coverage_fields"]["delivered_at"] = {"start": "2023-12-01", "end": "2026-01-01"}
    pending = missing_year(analysis(manifest, "details")).model_copy(update={"time_field": "delivered_at"})
    assert choices(pending, manifest) == [(f"{year}年", f"{year}年") for year in (2023, 2024, 2025)]
