import csv
from decimal import Decimal

from app.demo import practice_plans
from app.models import QueryResult
from app.presentation import present


def test_export_and_chart_use_exact_result(executor, tmp_path, monkeypatch):
    monkeypatch.setattr("app.presentation.RUNS_DIR", tmp_path)
    directory = tmp_path / executor.manifest["dataset_id"]
    directory.mkdir()
    plan = practice_plans(executor.manifest)["Q05"]
    result = QueryResult(query_id="view-query", status="ok", columns=["entity_id", "entity_name", "delay_rate", "sample_orders"], rows=[["S1", "测试商家", 0.5, 12]])
    record = {"record_id": "view-query", "type": "query", "dataset_id": plan.dataset_id,
              "plan": plan.model_dump(mode="json"), "result": result.model_dump()}
    view = present(record)
    assert view.chart == [{"分组": "测试商家", "数值": 50.0}]
    assert view.table[0][2] == "50.00%"
    assert view.chart_kind == "bar"
    with open(view.csv_file, encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle))[0]["delay_rate"] == "0.5"


def test_money_export_keeps_cents(executor, tmp_path, monkeypatch):
    monkeypatch.setattr("app.presentation.RUNS_DIR", tmp_path)
    (tmp_path / executor.manifest["dataset_id"]).mkdir()
    plan = practice_plans(executor.manifest)["Q02"]
    result = QueryResult(query_id="money-query", status="ok", columns=["sales"], rows=[[Decimal("75484.21")]])
    record = {"record_id": "money-query", "type": "query", "dataset_id": plan.dataset_id,
              "plan": plan.model_dump(mode="json"), "result": result.model_dump()}
    view = present(record)
    assert view.chart == [] and view.table == [["75484.21"]]
    with open(view.csv_file, encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == [{"sales": "75484.21"}]


def test_reset_removes_previous_downloads():
    view = present({})
    assert view.csv_file is None and view.evidence_file is None and not view.table
