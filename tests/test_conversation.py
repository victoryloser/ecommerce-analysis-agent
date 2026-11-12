import json

import pytest
from pydantic import ValidationError

from app.demo import practice_plans
from app.models import TimeRange
from app.orchestrator import AnalysisAgent
from app.planner import make_plan
from app.session import Session
from app.trace import TurnTrace


class ScriptedProvider:
    """只验证协调器状态流，不计作真实模型理解能力。"""
    model = "test-scripted-provider"

    def __init__(self, replies):
        self.replies = iter(replies)
        self.inputs = []

    def complete(self, messages):
        self.inputs.append(messages.copy())
        reply = next(self.replies)
        return reply if isinstance(reply, str) else reply.model_dump_json()


def test_success_clarify_failure_and_explanation_keep_success(executor):
    plans = practice_plans(executor.manifest)
    clarify = plans["Q02"].model_copy(update={"intent": "clarify", "time_range": None, "clarification": "要查询哪一年的4月？"})
    outside = plans["Q02"].model_copy(update={"time_range": TimeRange(start="2030-01-01", end="2030-02-01")})
    provider = ScriptedProvider([plans["Q02"], clarify, outside, plans["Q08"]])
    agent = AnalysisAgent(provider, {executor.manifest["dataset_id"]: executor})
    session = Session(executor.manifest["dataset_id"])
    agent.respond(session, "全期销售额")
    successful = session.last_successful_plan
    last_result = session.last_result
    agent.respond(session, "4月呢")
    assert session.pending_plan == clarify and session.last_successful_plan == successful
    agent.respond(session, "2030年")
    assert session.pending_plan is None and session.last_successful_plan == successful
    turn = agent.respond(session, "为什么关联重复")
    assert session.last_result == last_result and turn.evidence["result"] is None
    context = json.loads(provider.inputs[2][1]["content"])
    assert context["pending_plan"]["clarification"] == clarify.clarification
    assert context["last_successful_plan"]["metric_ids"] == ["sales"]
    agent.respond(session, "重新开始")
    assert session.last_successful_plan is None and session.pending_plan is None and not session.history


def test_clarification_then_completed_plan(executor):
    plan = practice_plans(executor.manifest)["Q03"]
    pending = plan.model_copy(update={"intent": "clarify", "time_range": None, "comparison_range": None, "clarification": "哪一年的4月？"})
    provider = ScriptedProvider([pending, plan])
    session = Session(executor.manifest["dataset_id"])
    agent = AnalysisAgent(provider, {session.dataset_id: executor})
    first = agent.respond(session, "4月比3月呢")
    assert first.evidence["result"] is None
    second = agent.respond(session, "2025年")
    assert second.evidence["result"]["status"] == "ok"
    assert session.last_successful_plan == plan and session.pending_plan is None


def test_structure_repair_is_bounded(executor):
    provider = ScriptedProvider(["not json"] * 3)
    with pytest.raises(ValidationError):
        make_plan(provider, executor.manifest, Session(executor.manifest["dataset_id"]), "销售额", False, TurnTrace("format-test", executor.manifest["dataset_id"], "销售额", provider.model))
    assert len(provider.inputs) == 3


def test_structure_repair_returns_to_full_validation(executor):
    plan = practice_plans(executor.manifest)["Q01"]
    provider = ScriptedProvider(["{}", plan])
    parsed, attempts = make_plan(provider, executor.manifest, Session(executor.manifest["dataset_id"]), "全期订单数", True, TurnTrace("repair-test", executor.manifest["dataset_id"], "全期订单数", provider.model))
    assert parsed == plan and attempts == 2


def test_unrelated_session_has_no_previous_context(executor):
    plan = practice_plans(executor.manifest)["Q01"]
    provider = ScriptedProvider([plan])
    make_plan(provider, executor.manifest, Session(executor.manifest["dataset_id"]), "全期订单数", False, TurnTrace("isolation-test", executor.manifest["dataset_id"], "全期订单数", provider.model))
    context = json.loads(provider.inputs[0][1]["content"])
    assert context["last_successful_plan"] is None and context["recent_messages"] == []
