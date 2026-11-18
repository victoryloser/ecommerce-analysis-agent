import csv
import json
from pathlib import Path

import httpx
import pytest

from app import cli
from app.calculation import ROUND2_RULE
from app.executor import Executor
from app.import_data import import_tables
from app.models import AnalysisPlan, PlanUpdate
from app.orchestrator import AnalysisAgent
from app.session import Session
from app.session_store import SessionStore
from app.trace import TurnTrace
from test_r2_evaluation import isolated_runs
from test_round2_amounts import plan_for, source_tables
from test_semifinal_state import Scripted


@pytest.fixture(scope="module")
def large_snapshot(tmp_path_factory):
    directory = tmp_path_factory.mktemp("followup-business")
    tables = source_tables(False)
    for index in range(10):
        key = f"G{index:02d}"
        tables["categories"].append({**tables["categories"][0], "category_id": key, "category_name_zh": key})
        tables["products"].append({**tables["products"][0], "product_id": key, "category_id": key})
        tables["order_items"].append({**tables["order_items"][1], "order_item_id": index+3,
                                     "product_id": key, "price": 40 if index < 2 else 10+index})
    for index in range(1022):
        order_id = f"T{index:04d}"
        tables["orders"].append({**tables["orders"][1], "order_id": order_id, "purchase_at": "2027-04-30"})
        tables["order_items"].append({**tables["order_items"][1], "order_id": order_id, "price": 1})
    manifest = import_tables("followup", tables, [], "Synthetic follow-up", directory, quality_rule_version=ROUND2_RULE)
    return Executor(manifest["dataset_id"], directory)


def test_complete_contribution_survives_model_omission_and_scope_changes(large_snapshot, isolated_runs):
    manifest = large_snapshot.manifest
    base = plan_for(manifest, intent="compare", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    updates = [PlanUpdate(operation="set", field="intent", values=["contribution"]),
               PlanUpdate(operation="set", field="dimension", values=["category"]),
               PlanUpdate(operation="set", field="metric_ids", values=["sales"])]
    update = AnalysisPlan(dataset_id=base.dataset_id, metadata_version=base.metadata_version, intent="update", updates=updates)
    agent = AnalysisAgent(Scripted(base, update, update, update), {base.dataset_id: large_snapshot})
    session = Session(base.dataset_id)
    agent.respond(session, "比较三月和四月")
    question = "沿用刚才范围，逐类列出三月和四月的差额"
    trace = TurnTrace(session.session_id, session.dataset_id, question, agent.provider.model)
    complete = agent.respond(session, question, trace=trace)
    limited = agent.respond(session, "按全品类计算贡献分母，只显示差额前三")
    restored = agent.respond(session, "恢复全量")
    assert trace.model_calls == 1
    assert [t.evidence["plan"]["limit_mode"] for t in (complete, limited, restored)] == ["all", "top_n", "all"]
    assert len(complete.evidence["result"]["rows"]) == len(restored.evidence["result"]["rows"]) == 12
    assert [r[0] for r in limited.evidence["result"]["rows"]] == ["A", "B", "G00", "G01"]
    totals = [{row[result["columns"].index("total_change")] for row in result["rows"]}
              for result in (t.evidence["result"] for t in (complete, limited, restored))]
    assert totals[0] == totals[1] == totals[2]
    events = [json.loads(line) for line in trace.event_file.read_text(encoding="utf-8").splitlines()]
    request = next(e for e in events if e["turn_id"] == trace.turn_id and e["event"] == "result_selection_applied")
    assert request["before"] == {"limit_mode": "top_n"} and request["effective"] == {"limit_mode": "all"}


def test_contribution_selector_leaves_non_display_and_other_workflow_steps_alone(large_snapshot, isolated_runs):
    from app.result_selection import apply_result_selection
    plan = plan_for(large_snapshot.manifest, intent="contribution", dimension="category", metric_ids=["sales"], top_n=7)
    rank = plan.model_copy(update={"intent": "rank", "dimension": "seller", "top_n": 3})
    workflow = plan.model_copy(update={"intent": "workflow", "steps": [plan, rank]})
    trace = TurnTrace("selection", plan.dataset_id, "selection", "fixed")
    for question in ("不要展示全部品类", "按全部品类计算贡献率", "每个品类列出前三名商家"):
        assert apply_result_selection(workflow, question, trace) == workflow
    selected = apply_result_selection(workflow, "展示所有品类的贡献，列出前三名商家", trace)
    assert selected.steps[0].limit_mode == "all" and selected.steps[1] == rank
    selected = apply_result_selection(plan, "展示所有品类的贡献，列出前三名商家", trace)
    assert selected.limit_mode == "all"


def test_consecutive_pages_and_export_share_order_and_scope(large_snapshot, isolated_runs, tmp_path):
    plan = plan_for(large_snapshot.manifest, intent="details", metric_ids=[],
                    time_range={"start": "2027-03-01", "end": "2027-05-01"})
    store = SessionStore(tmp_path/"sessions.sqlite3")
    agent = AnalysisAgent(Scripted(plan), {plan.dataset_id: large_snapshot}, store)
    session = Session(plan.dataset_id)
    first = agent.respond(session, "列出两月订单明细").evidence
    # Simulate resuming an earlier record before page_size was stored.
    session.last_evidence["result"].pop("page_size")
    second = agent.page(session, 1, 500).evidence
    last = agent.page(session, 1).evidence
    records = [t["result"] for t in (first, second, last)]
    assert [(r["row_offset"], len(r["rows"])) for r in records] == [(0, 500), (500, 500), (1000, 25)]
    with pytest.raises(ValueError, match="最后一页"):
        agent.page(session, 1)
    resumed = store.load(session.session_id, large_snapshot.manifest)
    assert agent.page(resumed, -1).evidence["result"]["rows"] == records[1]["rows"]
    resized = agent.page(resumed, 0, 200).evidence["result"]
    assert resized["row_offset"] == 0 and resized["rows"] == records[0]["rows"][:200]
    following = agent.page(resumed, 1).evidence["result"]
    assert following["row_offset"] == 200 and following["rows"] == records[0]["rows"][200:400]
    with Path(agent.export(resumed)["csv_file"]).open(encoding="utf-8-sig", newline="") as handle:
        exported = [row["order_id"] for row in csv.DictReader(handle)]
    viewed = [row[r["columns"].index("order_id")] for r in records for row in r["rows"]]
    assert exported == viewed and len(set(viewed)) == 1025


@pytest.mark.parametrize("status,expected", [(None, "模型请求超时"), (429, "限流"), (403, "拒绝访问"), (503, "暂时不可用")])
def test_cli_retries_the_original_question_after_model_error(large_snapshot, isolated_runs, monkeypatch, tmp_path, capsys, status, expected):
    executor = large_snapshot
    dataset = executor.manifest["dataset_id"]
    seen = []
    class TemporaryFailure:
        model = "injected-model-failure"

        def complete(self, messages):
            context = json.loads(messages[1]["content"])
            seen.append((context["question"], context["analysis_anchor"]))
            if len(seen) == 2:
                if status is None:
                    raise httpx.ReadTimeout("private gateway detail")
                request = httpx.Request("POST", "https://provider.invalid/?key=private")
                httpx.Response(status, request=request).raise_for_status()
            start, end = ("2027-04-01", "2027-05-01") if len(seen) == 1 else ("2027-03-01", "2027-04-01")
            return plan_for(executor.manifest, time_range={"start": start, "end": end}).model_dump_json()

    store = SessionStore(tmp_path/"sessions.sqlite3")
    monkeypatch.setattr(cli, "registered_datasets", lambda: {dataset: "fixture"})
    monkeypatch.setattr(cli, "resolve_dataset", lambda value: value)
    monkeypatch.setattr(cli, "Executor", lambda value: executor)
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "ChatCompletionsProvider", TemporaryFailure)
    monkeypatch.setattr("sys.argv", ["cli"])
    inputs = iter(["查看四月订单", "改看三月", "/retry", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    cli.main()
    captured = capsys.readouterr().out
    assert expected in captured and "private" not in captured
    assert [q for q, _ in seen] == ["查看四月订单", "改看三月", "改看三月"]
    assert seen[1][1] == seen[2][1] and seen[1][1] is not None
