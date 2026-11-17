from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import zipfile

import pytest

from test_semifinal_quality import tables
from test_data_onboarding import write_business_files
from app.data_upload import registered_datasets
from app.executor import Executor
from app.import_data import import_dataset, import_tables
from app.models import AnalysisPlan, ComparisonRequest, EntityFilter, QueryResult, TimeRange
from app.orchestrator import AnalysisAgent
from app.session import Session
from app.session_store import SessionStore
from app.snapshots import resolve_dataset, read_retained
from app.oracle import SourceOracle


@pytest.fixture
def setup(tmp_path, tables, monkeypatch):
    import app.evidence as evidence
    import app.trace as trace
    import app.snapshot_comparison as comparison
    runs = tmp_path / "runs"
    monkeypatch.setattr(evidence, "RUNS_DIR", runs)
    monkeypatch.setattr(trace, "RUNS_DIR", runs)
    monkeypatch.setattr(comparison, "RUNS_DIR", runs)
    revised = deepcopy(tables)
    revised["order_items"][2]["price"] = 120
    revised["order_payments"][2]["payment_value"] = 120
    for name, data in (("old", tables), ("new", revised)):
        import_tables(name, data, [], name, tmp_path)
    agent = AnalysisAgent(None, {name: Executor(name, tmp_path) for name in ("old", "new")}, SessionStore(tmp_path / "sessions.sqlite3"))
    plan = AnalysisPlan(dataset_id="old", metadata_version="1.0.0", intent="compare", metric_ids=["sales"],
        time_range=TimeRange(start="2027-04-01", end="2027-05-01"), comparison_range=TimeRange(start="2027-03-01", end="2027-04-01"))
    session = Session("old", working_plan=plan)
    return agent, session


def test_comparison_recomputes_both_sides_and_evidence_bundle(setup):
    agent, session = setup
    turn = agent.compare(session, ComparisonRequest(target_dataset_id="new", source="working_plan"))
    state = turn.evidence
    assert state["status"] == "completed"
    assert state["original_record_id"] is None
    result = state["stages"]["diff"]["report"]["reconciliation"][0]
    assert Decimal(result["revision_effect"]) == 20 and result["verified"]
    assert session.last_successful_plan is None and not session.analysis_evidence
    with zipfile.ZipFile(state["bundle_file"]) as archive:
        assert set(archive.namelist()) == {"report.json", "diff.json", "differences.csv", "source_evidence.json", "target_evidence.json"}
        before, after = [json.loads(archive.read(side + "_evidence.json")) for side in ("source", "target")]
        assert before["dataset_id"] == "old" and after["dataset_id"] == "new"
        assert before["implementation_sha256"] == after["implementation_sha256"] == state["implementation_sha256"]
    restored = agent.store.load(session.session_id, agent.executors["old"].manifest)
    assert restored.comparison_state["comparison_id"] == state["comparison_id"]
    hits = agent.store.search("比较数据版本", agent.executors["old"].manifest)
    assert hits[0]["evidence_id"] == state["artifact_id"]


def test_failed_stage_resume_preserves_source_evidence(setup, monkeypatch):
    agent, session = setup
    executor = agent.executors["new"]
    execute = executor.execute_plan
    def timeout(*args, **kwargs):
        result, compiled = execute(*args, **kwargs)
        return QueryResult(query_id=result.query_id, status="timeout", error="构造超时"), compiled
    monkeypatch.setattr(executor, "execute_plan", timeout)
    state = agent.compare(session, {"target_dataset_id": "new", "source": "working_plan"}).evidence
    assert state["status"] == "incomplete" and state["stages"]["source"]["status"] == "completed"
    record = state["stages"]["source"]["evidence"]["record_id"]
    monkeypatch.setattr(executor, "execute_plan", execute)
    restored = agent.store.load(session.session_id, agent.executors["old"].manifest)
    state = agent.compare(restored, resume=True).evidence
    assert state["status"] == "completed"
    assert state["stages"]["source"]["evidence"]["record_id"] == record


def test_failed_diff_resume_and_new_working_intent_not_old_success(setup, monkeypatch):
    import app.snapshot_comparison as comparison
    agent, session = setup
    old_plan = session.working_plan
    fresh, _ = agent.replay(session, "old")
    session = fresh
    session.working_plan = old_plan.model_copy(update={"filters": [EntityFilter(field="category_id", values=["K2"])]})
    real = comparison.run_diff
    monkeypatch.setattr(comparison, "run_diff", lambda *a, **k: {"status": "timeout", "error": "构造差异阶段超时"})
    state = agent.compare(session, {"target_dataset_id": "new", "source": "working_plan"}).evidence
    assert state["status"] == "incomplete"
    assert state["stages"]["source"]["effective_plan"]["filters"][0]["values"] == ["K2"]
    ids = [state["stages"][side]["evidence"]["record_id"] for side in ("source", "target")]
    monkeypatch.setattr(comparison, "run_diff", real)
    state = agent.compare(session, resume=True).evidence
    assert state["status"] == "completed"
    assert ids == [state["stages"][side]["evidence"]["record_id"] for side in ("source", "target")]


def test_fixed_scope_closes_dynamic_date_modes(setup):
    agent, session = setup
    session.working_plan = session.working_plan.model_copy(update={"intent": "aggregate", "comparison_range": None,
        "period_mode": "full_coverage", "time_range": TimeRange(start="2027-03-01", end="2027-05-01")})
    fresh, _ = agent.replay(session, "old")
    agent.executors["new"].manifest["coverage"]["end"] = "2027-06-01"
    agent.executors["new"].manifest["coverage_fields"]["purchase_at"]["end"] = "2027-06-01"
    fixed = agent.compare(fresh, {"target_dataset_id": "new"}).evidence
    assert fixed["stages"]["target"]["effective_plan"]["period_mode"] == "explicit_range"
    assert fixed["stages"]["target"]["effective_plan"]["time_range"]["end"] == "2027-05-01"
    replay = agent.compare(fresh, {"target_dataset_id": "new", "mode": "intent_replay", "source": "working_plan"}).evidence
    assert replay["stages"]["target"]["effective_plan"]["time_range"]["end"] == "2027-06-01"
    assert "time_range" in replay["stages"]["diff"]["report"]["scope_changes"]


def test_replay_records_history_and_provenance(setup):
    agent, session = setup
    fresh, turn = agent.replay(session, "new")
    assert len(fresh.transcript) == 2
    assert agent.store.list("new")[0]["title"] == "在新快照重算"
    assert turn.evidence["provenance"]["source_session_id"] == session.session_id
    with pytest.raises(ValueError, match="请先建立或恢复来源分析"):
        agent.compare(None, {"target_dataset_id": "new"})


def test_workflow_reselects_leader_and_fixed_result_keeps_entity(setup, tables, tmp_path):
    agent, session = setup
    data = deepcopy(tables)
    for row in data["order_items"]:
        row["product_id"] = "K2"
    manifest = import_tables("leader", data, [], "leader", tmp_path)
    agent.executors["leader"] = Executor("leader", tmp_path)
    rank = session.working_plan.model_copy(update={"intent": "rank", "dimension": "category", "comparison_range": None, "top_n": 1})
    follow = rank.model_copy(update={"intent": "aggregate", "dimension": "total", "source_step": 1, "bind_field": "category_id"})
    session.working_plan = rank.model_copy(update={"intent": "workflow", "steps": [rank, follow]})
    session.focus_step = 2
    state = agent.compare(session, {"target_dataset_id": "leader", "source": "working_plan", "mode": "intent_replay"}).evidence
    assert state["status"] == "completed"
    assert state["stages"]["source"]["effective_plan"]["filters"][0]["values"] == ["K1"]
    assert state["stages"]["target"]["effective_plan"]["filters"][0]["values"] == ["K2"]
    assert "filters" in state["stages"]["diff"]["report"]["scope_changes"]
    assert all(r["delta"] is None for r in state["stages"]["diff"]["report"]["metric_comparisons"])
    session.analysis_evidence = state["stages"]["source"]["evidence"]
    fixed = agent.compare(session, {"target_dataset_id": "leader"}).evidence
    assert fixed["stages"]["target"]["effective_plan"]["filters"][0]["values"] == ["K1"]


def test_rules_mismatch_and_ui_comparison_without_provider(setup, monkeypatch):
    import app.ui as ui
    agent, session = setup
    agent.executors["new"].manifest["quality_rule_version"] = "different-rule"
    monkeypatch.setattr(ui, "registered_datasets", lambda: {key: key for key in agent.executors})
    monkeypatch.setattr(ui, "Executor", lambda key: agent.executors[key])
    monkeypatch.setattr(ui, "SessionStore", lambda: agent.store)
    demo = ui.build_ui()
    callback = next(fn.fn for fn in demo.fns.values() if fn.api_name == "compare_current")
    output = callback(session, "new", "fixed_scope", "working_plan")
    assert "口径不同" in output[2]
    state = output[7]
    assert state["status"] == "completed"
    assert "quality_rule_version" in state["contract_changes"]
    assert all(r["delta"] is None for r in state["stages"]["diff"]["report"]["metric_comparisons"])


def test_same_id_reimport_rejected_old_executor_unchanged(tmp_path, tables):
    manifest = import_tables("fixed", tables, [], "fixed", tmp_path)
    executor = Executor("fixed", tmp_path)
    revised = deepcopy(tables)
    revised["order_items"][0]["price"] = 999
    with pytest.raises(ValueError, match="快照 ID 已存在"):
        import_tables("fixed", revised, [], "fixed", tmp_path)
    assert hashlib.sha256(executor.path.read_bytes()).hexdigest() == manifest["database_sha256"]


def test_builtin_alias_keeps_prior_source_and_oracle(tmp_path, monkeypatch):
    import app.import_data as importer
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    paths = write_business_files("csv", source_dir)
    import csv
    from pathlib import Path
    def load(_):
        result = {}
        for p in paths:
            with open(p, encoding="utf-8-sig") as handle:
                result[Path(p).stem] = list(csv.DictReader(handle))
        return result
    monkeypatch.setattr(importer, "read_source", load)
    monkeypatch.setattr(importer, "source_files", lambda _: [Path(p) for p in paths])
    directory = tmp_path / "data"
    first = import_dataset("csv_small", directory)
    path = source_dir / "order_items.csv"
    path.write_text(path.read_text(encoding="utf-8-sig").replace("12.50", "25.00"), encoding="utf-8-sig")
    second = import_dataset("csv_small", directory)
    assert first["dataset_id"] != second["dataset_id"]
    assert resolve_dataset("csv_small", directory) == second["dataset_id"]
    original = read_retained(first, directory)
    assert Decimal(original["order_items"][0]["price"]) == Decimal("12.50")
    assert len(registered_datasets(directory)) == 2
    plan = AnalysisPlan(dataset_id=first["dataset_id"], metadata_version="1.0.0", intent="aggregate", metric_ids=["sales"],
        time_range=TimeRange(start="2025-01-01", end="2025-02-01"))
    assert SourceOracle(first["dataset_id"], data_dir=directory).evaluate(plan)[0]["sales"] == Decimal("12.50")
