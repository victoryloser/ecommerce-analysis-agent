import csv
from copy import deepcopy
from decimal import Decimal

import pytest

from app.calculation import ROUND2_RULE
from app.executor import Executor
from app.import_data import import_tables
from app.orchestrator import AnalysisAgent
from app.review import build_review, compare_review, load_review, review_drill, review_record, review_sources
from app.review_ui import contribution_text, overview_text, version_view
from app.semantic import PlanError
from app.session import Session
from app.session_store import SessionStore
from app.source import SCHEMA
from test_r2_evaluation import isolated_runs
from test_round2_amounts import plan_for, source_tables
from test_semifinal_state import Scripted


def make_snapshot(directory, name, tables):
    source = directory / name
    source.mkdir()
    files = []
    for table, rows in tables.items():
        path = source / f"{table}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA[table])
            writer.writeheader()
            writer.writerows(rows)
        files.append(path)
    manifest = import_tables(name, tables, files, name, directory, quality_rule_version=ROUND2_RULE)
    return Executor(manifest["dataset_id"], directory)


def test_review_reuse_and_version_impact_trace_to_conflicting_rows(tmp_path, isolated_runs, monkeypatch):
    import app.snapshot_comparison
    monkeypatch.setattr(app.snapshot_comparison, "RUNS_DIR", isolated_runs)
    clean = source_tables(False)
    conflict = deepcopy(clean)
    conflict["order_items"].append({**clean["order_items"][1], "price": 120})
    source = make_snapshot(tmp_path, "before", clean)
    target = make_snapshot(tmp_path, "after", conflict)
    plan = plan_for(source.manifest, intent="compare", comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    next_plan = plan_for(source.manifest, time_range={"start": "2027-03-01", "end": "2027-04-01"})
    provider = Scripted(plan, next_plan)
    store = SessionStore(tmp_path / "sessions.sqlite3")
    agent = AnalysisAgent(provider, {"before": source, "after": target}, store)
    session = Session("before")
    first = agent.respond(session, "先比较两个月的商品额和订单").evidence
    card = build_review(agent, session, first)
    assert card["source_record_id"] == card["overview_id"] == first["record_id"]
    overview = review_record(agent, session, card, "overview_id")
    assert "| 商品销售额（元） | 40.00 | 190.00 | 150.00 |" in overview_text(overview, source.manifest)
    contribution = review_record(agent, session, card, "contribution_id")
    assert contribution["plan"]["limit_mode"] == "all"
    drill = review_drill(agent, session, card["record_id"], "A")
    assert drill["plan"]["dimension"] == "seller" and drill["plan"]["filters"] == [{"field": "category_id", "values": ["A"]}]
    assert session.analysis_anchor == first["record_id"] and provider.calls == 1

    second = agent.respond(session, "改成只看三月").evidence
    compared = compare_review(agent, session, card["record_id"], "after")
    version = compared["version"]
    sale = next(row for row in version["reconciliation"] if row["metric"] == "sales")
    assert [Decimal(n) for n in sale["values"]] == [40, 190, 40, 40]
    assert Decimal(sale["revision_effect"]) == -150 and sale["verified"]
    assert len(version["affected_orders"]) == 1
    affected = version["affected_orders"][0]
    assert affected["order_id"] == "O1" and affected["change"] == "eligibility_exited"
    assert affected["issues"] == [{"item_key": ["O1", 1], "change": "added"}]
    assert Decimal(affected["amounts"]["sales"]) == -150
    sources = review_sources(agent, session, compared["record_id"], 0)
    prices = {row["values"]["price"] for row in sources["records"] if row["table"] == "order_items" and row["values"]["order_item_id"] == "1"}
    assert sources["dataset"] == "after" and prices == {"100", "120"}
    description, _, _, _ = version_view(version, plan)
    assert "-150.00" in description and "资格退出 150.00" in description
    assert session.working_plan == next_plan and session.analysis_anchor == second["record_id"]
    assert session.last_evidence == second and session.comparison_state == {} and provider.calls == 2
    assert len(session.transcript) == 4

    restored = store.load(session.session_id, source.manifest)
    assert list(restored.reviews) == [card["record_id"], compared["record_id"]]
    assert load_review(agent, restored, compared["record_id"]) == compared
    assert store.turn_evidence(restored.session_id, 0, source.manifest)["record_id"] == first["record_id"]
    assert store.turn_evidence(restored.session_id, 1, source.manifest)["record_id"] == second["record_id"]
    with pytest.raises(ValueError, match="已改变"):
        store.turn_evidence(restored.session_id, 0, target.manifest)
    with pytest.raises(PlanError, match="当前会话"):
        load_review(agent, Session("before"), card["record_id"])


def test_review_keeps_signed_changes_when_net_is_zero(tmp_path, isolated_runs):
    tables = source_tables(False)
    tables["order_items"][0]["price"] = 190
    executor = make_snapshot(tmp_path, "zero", tables)
    plan = plan_for(executor.manifest, intent="compare", metric_ids=["sales"], comparison_range={"start": "2027-03-01", "end": "2027-04-01"})
    provider = Scripted(plan)
    agent = AnalysisAgent(provider, {"zero": executor})
    session = Session("zero")
    first = agent.respond(session, "两月商品额分别是多少").evidence
    card = build_review(agent, session, first)
    assert card["overview_id"] != card["source_record_id"]
    record = review_record(agent, session, card, "contribution_id")
    assert "净差额为零，贡献率未定义" in contribution_text(record)
    assert {Decimal(row[record["result"]["columns"].index("sales_change")]) for row in record["result"]["rows"]} == {-100, 100}
    assert provider.calls == 1
