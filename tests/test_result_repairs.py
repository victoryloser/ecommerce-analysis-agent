import csv
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, sql_workspace, team
from app.executor import Executor
from app.models import AnalysisPlan
from app.orchestrator import AnalysisAgent
from app.session import Session
from app.session_store import archive_connection
from app.sql_workspace import SQLWorkspace
from test_weekly import ReportPlanner, pilot


@pytest.mark.parametrize("scope,sql,parameter", [
    ("raw", "SELECT order_id FROM orders WHERE order_id = ?", "o1"),
    ("analysis_result", "SELECT sales FROM analysis_result WHERE order_count > ?", 2),
])
def test_export_uses_executed_sql_after_new_draft(pilot, monkeypatch, scope, sql, parameter):
    for module in (config, sql_workspace):
        monkeypatch.setattr(module, "RUNS_DIR", pilot.root / "runs")
    dataset = pilot.snapshot()
    agent = AnalysisAgent(ReportPlanner(), {dataset: Executor(dataset, pilot.db)})
    workspace = SQLWorkspace(agent.executors)
    session = Session(dataset)
    agent.respond(session, "商品额及订单")
    record = workspace.execute(session, sql, [parameter], scope)
    assert record["result"]["status"] == "ok" and len(record["result"]["rows"]) == 1
    class Draft:
        model = "test"

        def complete(self, messages):
            return json.dumps({"sql": "SELECT 3 AS another", "explanation": "新草稿"})

    workspace.draft(session, "另存一个草稿", scope, Draft())
    for legacy in (False, True):
        if legacy:
            del record["user_parameters"]
        exported = agent.export(session)
        assert exported["source_record_id"] == record["record_id"]
        with Path(exported["csv_file"]).open(encoding="utf-8-sig", newline="") as stream:
            assert list(csv.reader(stream)) == [record["result"]["columns"], [str(v) for v in record["result"]["rows"][0]]]
    page = agent.page(session, 0, page_size=1).evidence
    assert page["result"]["rows"] == record["result"]["rows"] and page["user_parameters"] == [parameter]
    assert session.sql_state["sql"] == "SELECT 3 AS another"


def test_order_scoped_share_follows_current_permissions_and_legacy_plan(pilot):
    class ScopedPlanner(ReportPlanner):
        def complete(self, messages):
            plan = AnalysisPlan.model_validate_json(super().complete(messages))
            if json.loads(messages[1]["content"])["question"] == "核对指定订单":
                plan.selected_order_id = "o1"
            return plan.model_dump_json()

    p = pilot
    p.client = TestClient(team.create_app(p.db, ScopedPlanner))
    dataset = p.snapshot()
    def share(question, recipient):
        turn = p.ok("/api/action", dict(dataset=dataset, value=question), "admin")
        return p.post("/api/action", dict(dataset=dataset, session=turn["session"], action="share", target=recipient), "admin")

    assert share("核对指定订单", "reader").status_code == 422
    scoped = share("核对指定订单", "finance").json()["report"]
    general = share("全店汇总", "finance").json()["report"]
    assert p.get("/api/reports/" + scoped, "finance").json()["selected_order_id"] == "o1"
    p.access.change_access("finance", "analyst", [], ["*"])
    def check_access():
        assert p.get("/api/reports/" + scoped, "finance").status_code == 404
        assert p.get("/api/reports/" + general, "finance").is_success
        assert [r["id"] for r in p.get("/api/home", "finance").json()["reports"]] == [general]
    check_access()
    with archive_connection(p.access.path) as db:
        rows = db.execute("SELECT id,payload FROM resources WHERE kind='report'").fetchall()
        for key, saved in rows:
            previous = json.loads(saved)
            del previous["selected_order_id"], previous["output_mode"]
            db.execute("UPDATE resources SET required='[]',payload=? WHERE id=?", (json.dumps(previous), key))
    check_access()


def test_team_reset_works_without_model_configuration(pilot):
    def unavailable():
        pytest.fail("本地清空命令不应初始化模型")

    dataset = pilot.snapshot()
    pilot.client = TestClient(team.create_app(pilot.db, unavailable))
    assert "已清空" in pilot.ok("/api/action", dict(dataset=dataset, value="/reset"))["answer"]
