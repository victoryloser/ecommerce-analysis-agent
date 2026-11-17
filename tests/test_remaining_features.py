import csv
import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.charts import render_charts
from app.config import ROOT
from app.data_upload import discover, import_upload, suggest_mapping
from app.demo import practice_plans
from app.executor import Executor
from app.models import AnalysisPlan, TimeRange
from app.orchestrator import AnalysisAgent
from app.presentation import display_evidence
from app.session import Session
from app.session_store import SessionStore
from app.source import SCHEMA, read_source
from app.sql_workspace import SQLWorkspace


class Scripted:
    model = "extended-scripted"
    def __init__(self, plans):
        self.plans = iter(plans)
        self.calls = 0
    def complete(self, messages):
        self.calls += 1
        return next(self.plans).model_dump_json()


def test_workflow_runs_queries_then_explains_from_same_evidence(executor, tmp_path):
    plans = practice_plans(executor.manifest)
    comparison = plans["Q03"]
    contribution = comparison.model_copy(update={"intent": "contribution", "dimension": "category"})
    explain = plans["Q08"].model_copy(update={"explanation_topic": "causality"})
    workflow = plans["Q02"].model_copy(update={"intent": "workflow", "steps": [comparison, contribution, explain]})
    provider = Scripted([workflow])
    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = Session(workflow.dataset_id)
    agent = AnalysisAgent(provider, {session.dataset_id: executor}, store)
    turn = agent.respond(session, "比较3月和4月，再算各品类增长贡献，判断能否归因促销")
    assert turn.evidence["type"] == "workflow" and len(turn.evidence["steps"]) == 3
    assert provider.calls == 1 and turn.evidence["calls_used"] == 3
    assert turn.evidence["steps"][0]["result"]["status"] == "ok"
    assert turn.evidence["steps"][2]["result"] is None
    assert "没有促销" in turn.text
    assert display_evidence(turn.evidence)["plan"]["intent"] == "compare"
    restored = store.load(session.session_id, executor.manifest)
    assert restored.last_evidence["type"] == "workflow" and restored.last_successful_plan == comparison
    assert restored.working_plan == workflow and restored.focus_step == 1


def test_history_search_retrieves_old_turn_beyond_prompt_window(executor, tmp_path):
    store = SessionStore(tmp_path / "sessions.sqlite3")
    session = Session(executor.manifest["dataset_id"])
    session.remember("复购客户数是多少", "自然客户复购结果：46人，证据old-record")
    for i in range(8):
        session.remember(f"第{i}个其他问题", "其他话题")
    store.save(session, executor.manifest)
    assert len(session.history) == 8
    hits = SessionStore(store.path).search("回头客", executor.manifest)
    assert hits and hits[0]["question"] == "复购客户数是多少" and "46" in hits[0]["answer"]
    assert not store.search("回头客", executor.manifest, start=date.today()+timedelta(days=1))
    history = practice_plans(executor.manifest)["Q02"].model_copy(update={"intent": "history", "history_query": "回头客"})
    other = Session(session.dataset_id)
    turn = AnalysisAgent(Scripted([history]), {session.dataset_id: executor}, store).respond(other, "找以前那次回头客结果")
    assert turn.evidence["type"] == "history" and "46" in turn.text
    assert other.last_successful_plan is None


def test_history_date_filter_uses_local_day(executor, tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    store=SessionStore(tmp_path/"dates.sqlite3")
    session=Session(executor.manifest["dataset_id"])
    session.remember("当地凌晨销售额","按本机日历筛选")
    store.save(session,executor.manifest)
    local=datetime.now().astimezone().replace(hour=0,minute=10,second=0,microsecond=0)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE memories SET created_at=?",(local.astimezone(timezone.utc).isoformat(),))
    assert store.search("",executor.manifest,start=local.date(),end=local.date()+timedelta(days=1))


def test_workflow_uses_real_top_category_to_bind_next_query(executor, tmp_path):
    from app.oracle import SourceOracle, check_rows
    plans = practice_plans(executor.manifest)
    first = plans["Q03"].model_copy(update={"intent": "contribution", "dimension": "category"})
    second = plans["Q04"].model_copy(update={"dimension": "seller", "source_step": 1, "bind_field": "category_id", "top_n": 3})
    workflow = plans["Q02"].model_copy(update={"intent": "workflow", "steps": [first, second]})
    session = Session(workflow.dataset_id)
    turn = AnalysisAgent(Scripted([workflow]), {session.dataset_id: executor}).respond(session, "增长贡献最大的品类中，销售额前三的商家是谁")
    source, target = turn.evidence["steps"]
    actual_id = source["result"]["rows"][0][0]
    assert target["plan"]["filters"] == [{"field": "category_id", "values": [actual_id]}]
    from app.models import QueryResult
    check_rows(QueryResult.model_validate(target["result"]), SourceOracle(session.dataset_id).evaluate(AnalysisPlan.model_validate(target["plan"])))


def test_pagination_full_export_and_sql_result_check(executor, tmp_path):
    plan = practice_plans(executor.manifest)["Q02"].model_copy(update={"intent": "details", "metric_ids": []})
    session = Session(plan.dataset_id)
    agent = AnalysisAgent(Scripted([plan]), {session.dataset_id: executor}, SessionStore(tmp_path / "sessions.sqlite3"))
    agent.respond(session, "列出全期已送达订单明细")
    agent.page(session, 0, 50)
    first = session.last_result.rows
    agent.page(session, 1, 50)
    second = session.last_result.rows
    assert session.last_result.row_offset == 50 and len(first) == len(second) == 50
    assert not {row[0] for row in first} & {row[0] for row in second}
    record = agent.export(session)
    with Path(record["csv_file"]).open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    expected = sum(order["order_status"] == "delivered" for order in read_source(session.dataset_id)["orders"])
    assert record["row_count"] == len(rows) == expected
    workspace = SQLWorkspace(agent.executors)
    check = workspace.verify(session, "SELECT * FROM analysis_result", [], "analysis_result")
    assert check["matched"] and check["differing_rows"] == 0
    mismatch = workspace.verify(session, "SELECT * FROM analysis_result LIMIT 1", [], "analysis_result")
    assert not mismatch["matched"] and mismatch["differing_rows"] == expected-1


def test_order_click_subtables_and_delivery_status_click(executor, tmp_path):
    plans = practice_plans(executor.manifest)
    session = Session(executor.manifest["dataset_id"])
    agent = AnalysisAgent(Scripted([plans["Q06"]]), {session.dataset_id: executor})
    agent.respond(session, "按履约状态统计平均分")
    child = agent.drill(session, 0, "orders")
    assert child.evidence["plan"]["intent"] == "details"
    oid = child.evidence["result"]["rows"][0][2]
    detail = agent.drill(session, 0, "items")
    assert detail.evidence["plan"]["selected_order_id"] == oid
    assert all(row[2] == oid for row in detail.evidence["result"]["rows"])


def test_clickable_charts_escape_labels_and_keep_real_ids():
    evidence = {"type": "query", "plan": AnalysisPlan(dataset_id="csv_small",metadata_version="1.0.0",intent="aggregate",metric_ids=["sales","order_count"],dimension="category").model_dump(mode="json"),
        "result": {"status": "ok", "columns": ["entity_id", "entity_name", "sales", "order_count"], "rows": [['id"1', '<script>bad</script>', '10.00', 2]]}}
    html = render_charts(evidence, ["sales", "order_count"])
    assert '<script>bad</script>' not in html and '&lt;script&gt;' in html
    assert 'data-group-id="id&quot;1"' in html and html.count('<section') == 2


def test_upload_with_unknown_names_and_column_mapping(tmp_path, local_csv_dataset):
    files, mapping = [], {"tables": {}}
    for index, (table, columns) in enumerate(SCHEMA.items()):
        original = ROOT / "material/ecommerce_agent_demo_csv" / (table+".csv")
        with original.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        renamed = {field: f"field_{number}" for number, field in enumerate(columns)}
        target = tmp_path / f"external_{index}.csv"
        with target.open("w",encoding="utf-8",newline="") as handle:
            writer = csv.DictWriter(handle,fieldnames=list(renamed.values()))
            writer.writeheader()
            writer.writerows({renamed[key]: value for key,value in row.items()} for row in rows)
        files.append(str(target))
        mapping["tables"][table] = {"file":target.name,"sheet":None,"columns":renamed}
    structure = discover(files)
    assert len(structure) == 9 and len(suggest_mapping(structure)["tables"]) == 9
    manifest = import_upload(files,mapping,"外部经营数据",tmp_path/"data")
    assert manifest["name"] == "外部经营数据"
    executor = Executor(manifest["dataset_id"],tmp_path/"data")
    plan = AnalysisPlan(dataset_id=manifest["dataset_id"],metadata_version=manifest["metadata_version"],intent="aggregate",metric_ids=["sales"],
                        time_range=TimeRange(start=manifest["coverage"]["start"],end=manifest["coverage"]["end"]))
    session = Session(plan.dataset_id)
    result,_ = executor.execute_plan(session,executor.register(session,plan))
    assert result.status == "ok" and Decimal(str(result.rows[0][0])) == Decimal("75484.21")
    assert manifest["row_counts"]["orders"] == 180
    with pytest.raises(ValueError,match="完整9张"):
        import_upload(files,{"tables":{}},"missing",tmp_path/"other")
