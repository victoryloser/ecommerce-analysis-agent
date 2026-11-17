from copy import deepcopy

import pytest

from app.executor import Executor
from app.models import AnalysisPlan, EntityFilter, PlanUpdate, Predicate, QueryResult, TimeRange
from app.orchestrator import AnalysisAgent
from app.plan_update import apply_update, replay_intent
from app.session import Session
from app.session_store import SessionStore
from app.sql_compiler import CompiledQuery
from app.trace import TurnTrace, BudgetError
from app.workflow import bind_step
from test_semifinal_quality import tables, build


class Scripted:
    model = "p0-offline"

    def __init__(self, *plans):
        self.plans = iter(plans)
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        item = next(self.plans)
        return item if isinstance(item, str) else item.model_dump_json()


@pytest.fixture
def setup(tmp_path, tables, monkeypatch):
    manifest = build(tmp_path, tables)
    executor = Executor("fixture", tmp_path)
    plan = AnalysisPlan(dataset_id="fixture", metadata_version=manifest["metadata_version"], intent="aggregate",
                        metric_ids=["sales"], time_range=TimeRange(start="2027-04-01", end="2027-05-01"))
    calls = []
    failures = set()
    def execute(session, plan_id, trace=None):
        p = session.registered_plans.pop(plan_id)
        calls.append(p)
        failed = len(calls) in failures
        result = QueryResult(query_id=f"step-{len(calls)}", status="timeout" if failed else "ok",
                             columns=["sales"], rows=[] if failed else [[100]], error="injected timeout" if failed else None)
        return result, CompiledQuery("SELECT 100 AS sales", ())
    monkeypatch.setattr(executor, "execute_plan", execute)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    return executor, plan, calls, failures, store


def test_working_intent_survives_failure_and_success_stays_separate(setup):
    executor, plan, calls, failures, store = setup
    modified = plan.model_copy(update={"time_range": TimeRange(start="2027-03-01", end="2027-04-01")})
    failures.add(2)
    agent = AnalysisAgent(Scripted(plan, modified), {"fixture": executor}, store)
    session = Session("fixture")
    agent.respond(session, "April")
    agent.respond(session, "March")
    restored = store.load(session.session_id, executor.manifest)
    assert restored.working_plan == modified and restored.last_successful_plan == plan
    agent.respond(restored, "/continue")
    assert calls[-1] == modified and agent.provider.calls == 2


def test_independent_steps_execute_and_continue_only_unfinished(setup):
    executor, plan, calls, failures, store = setup
    clarify = plan.model_copy(update={"intent": "clarify", "pending_intent": "aggregate", "month": 3,
        "time_range": None, "missing_slots": ["year"], "clarification": "年份？"})
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan, clarify, plan]})
    agent = AnalysisAgent(Scripted(workflow), {"fixture": executor}, store)
    session = Session("fixture")
    agent.respond(session, "multi")
    assert len(calls) == 2
    assert [t["status"] for t in session.workflow_tasks] == ["completed", "needs_input", "completed"]
    ids = [t["evidence"].get("record_id") for t in session.workflow_tasks]
    restored = store.load(session.session_id, executor.manifest)
    turn = agent.respond(restored, "2027年")
    assert len(calls) == 3 and agent.provider.calls == 1
    assert all(t["status"] == "completed" for t in restored.workflow_tasks)
    assert ids[0] == turn.evidence["steps"][0]["record_id"] and ids[2] == turn.evidence["steps"][2]["record_id"]


def test_failed_third_task_checkpoints_and_retry(setup):
    executor, plan, calls, failures, store = setup
    failures.add(3)
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan]*4})
    agent = AnalysisAgent(Scripted(workflow), {"fixture": executor}, store)
    session = Session("fixture")
    agent.respond(session, "four")
    restored = SessionStore(store.path).load(session.session_id, executor.manifest)
    assert [t["status"] for t in restored.workflow_tasks] == ["completed", "completed", "failed", "completed"]
    assert restored.focus_step == 1
    agent.respond(restored, "/continue")
    assert len(calls) == 5 and all(t["status"] == "completed" for t in restored.workflow_tasks)


def test_six_queries_fit_after_repairs_and_catalog(setup):
    executor, plan, calls, failures, store = setup
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan]*6})
    catalog = plan.model_copy(update={"intent": "load_catalog"})
    agent = AnalysisAgent(Scripted("bad", "bad", catalog, workflow), {"fixture": executor}, store)
    turn = agent.respond(Session("fixture"), "six")
    assert len(calls) == 6 and turn.evidence["calls_used"] == 11
    assert all(t["status"] == "completed" for t in turn.evidence["tasks"])


def test_replace_remove_clears_duplicate_sources_and_ambiguous_or_rejects(setup):
    executor, plan, *_ = setup
    plan = plan.model_copy(update={"filters": [EntityFilter(field="category_id", values=["K1"])],
        "item_where": Predicate(field="category_id", values=["K1"])})
    changed = apply_update(plan, PlanUpdate(operation="set", field="category_id", values=["K2"]), executor.manifest)
    assert changed.filters == [] and changed.item_where.values == ["K2"]
    removed = apply_update(changed, PlanUpdate(operation="remove", field="category_id"), executor.manifest)
    assert removed.item_where is None and not removed.filters
    ambiguous = plan.model_copy(update={"item_where": Predicate(conjunction="any", children=[Predicate(field="category_id", values=["K1"]), Predicate(field="price", operator="gt", values=[50])])})
    with pytest.raises(ValueError, match="OR/NOT"):
        apply_update(ambiguous, PlanUpdate(operation="remove", field="category_id"), executor.manifest)


def test_period_relation_replay_and_snapshot_identity(setup):
    executor, plan, calls, failures, store = setup
    plan = plan.model_copy(update={"intent": "compare", "comparison_mode": "previous_month", "comparison_range": TimeRange(start="2027-03-01", end="2027-04-01")})
    change = PlanUpdate(operation="set", field="time_range", period=TimeRange(start="2027-03-01", end="2027-04-01"))
    assert apply_update(plan, change, executor.manifest).comparison_range.start.isoformat() == "2027-02-01"
    fixed = plan.model_copy(update={"comparison_mode": "fixed"})
    assert apply_update(fixed, change, executor.manifest).comparison_range == fixed.comparison_range
    manifest = deepcopy(executor.manifest); manifest["coverage_fields"]["purchase_at"]["end"] = "2027-06-01"
    full = plan.model_copy(update={"period_mode": "full_coverage"})
    assert replay_intent(full, manifest).time_range.end.isoformat() == "2027-06-01"
    session = Session("fixture"); store.save(session, executor.manifest)
    for field in ("database_sha256", "quality_rule_version"):
        altered = {**executor.manifest, field: "changed"}
        with pytest.raises(ValueError, match="绑定"):
            store.load(session.session_id, altered)


def test_upstream_change_invalidates_only_dependents(setup):
    from app.workflow import reconcile_tasks
    executor, plan, *_ = setup
    dependent = plan.model_copy(update={"source_step": 1, "bind_field": "category_id"})
    previous = plan.model_copy(update={"intent": "workflow", "steps": [plan, dependent, plan]})
    changed = apply_update(previous, PlanUpdate(operation="set", field="category_id", values=["K2"], target_step=1), executor.manifest)
    tasks = [{"status": "completed", "reason": ""} for _ in range(3)]
    assert [t["status"] for t in reconcile_tasks(previous, changed, tasks)] == ["pending", "pending", "completed"]
    explicit = apply_update(previous, PlanUpdate(operation="set", field="category_id", values=["K2"], target_step=2), executor.manifest)
    assert explicit.steps[1].source_step is None and explicit.steps[1].bind_field is None


def test_tied_leader_requires_entity(setup):
    _, plan, *_ = setup
    step = plan.model_copy(update={"source_step": 1, "bind_field": "category_id"})
    task = {"status": "completed", "evidence": {"plan": {"dimension": "category", "intent": "rank", "metric_ids": ["sales"]},
        "result": {"status": "ok", "columns": ["entity_id", "sales", "rank_no"], "rows": [["K1", 100, 1], ["K2", 100, 1]]}}}
    bound, status, reason = bind_step(step, [task])
    assert bound is None and status == "needs_input" and "并列" in reason


def test_interruption_after_two_steps_retains_checkpoints(setup, monkeypatch):
    executor, plan, calls, failures, store = setup
    original = executor.execute_plan
    class Interrupted(BaseException):
        pass
    def crash(session, plan_id, trace=None):
        if len(calls) == 2:
            raise Interrupted()
        return original(session, plan_id, trace)
    monkeypatch.setattr(executor, "execute_plan", crash)
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan]*3})
    agent = AnalysisAgent(Scripted(workflow), {"fixture": executor}, store)
    session = Session("fixture")
    with pytest.raises(Interrupted):
        agent.respond(session, "interrupt")
    restored = store.load(session.session_id, executor.manifest)
    assert [t["status"] for t in restored.workflow_tasks] == ["completed", "completed", "pending"]
    monkeypatch.setattr(executor, "execute_plan", original)
    agent.respond(restored, "/continue")
    assert len(calls) == 3


def test_snapshot_replay_rebinds_new_leader_and_clears_old_sql(tmp_path, tables):
    first_manifest = build(tmp_path, tables, "first")
    changed = deepcopy(tables)
    changed["order_items"][2].update(product_id="K2", price=1000)
    build(tmp_path, changed, "second")
    executors = {name: Executor(name, tmp_path) for name in ("first", "second")}
    plan = AnalysisPlan(dataset_id="first", metadata_version=first_manifest["metadata_version"], intent="rank", dimension="category",
        metric_ids=["sales"], top_n=1, period_mode="full_coverage", time_range=TimeRange(start="2027-03-01", end="2027-05-01"))
    detail = plan.model_copy(update={"intent": "details", "dimension": "total", "metric_ids": [], "source_step": 1, "bind_field": "category_id"})
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan, detail]})
    agent = AnalysisAgent(Scripted(workflow), executors)
    session = Session("first")
    agent.respond(session, "leader and detail")
    assert session.workflow_evidence["steps"][1]["plan"]["filters"][0]["values"] == ["K1"]
    session.sql_state = {"sql": "old draft"}
    fresh, turn = agent.replay(session, "second")
    assert fresh.session_id != session.session_id and fresh.sql_state == {}
    assert turn.evidence["steps"][1]["plan"]["filters"][0]["values"] == ["K2"]
    assert fresh.working_plan.steps[1].source_step == 1 and fresh.working_plan.steps[1].filters == []
