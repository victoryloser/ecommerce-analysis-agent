from copy import deepcopy
import csv
import json

import httpx
import pytest

from app.calculation import ROUND2_RULE
from app.executor import Executor
from app.import_data import import_tables
from app.models import Predicate, TimeRange
from app.oracle import SourceOracle
from app.orchestrator import AnalysisAgent
from app.round2_evaluate import evaluate_round2, summarize
from app.round2_judge import judge, same_scope
from app.round2_suite import CASE_IDS, Round2Case, contract, selected_chains
from app.session import Session
from app.source import SCHEMA
from test_round2_amounts import source_tables
from test_semifinal_state import Scripted


@pytest.fixture
def isolated_runs(tmp_path, monkeypatch):
    import app.config, app.evidence, app.trace, app.presentation, app.round2_evaluate, app.evaluate
    directory = tmp_path/"runs"
    for module in (app.config, app.evidence, app.trace, app.presentation, app.round2_evaluate, app.evaluate):
        monkeypatch.setattr(module, "RUNS_DIR", directory)
    return directory


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    directory = tmp_path_factory.mktemp("r2-source")
    tables = source_tables()
    for seller in ("C", "D"):
        tables["sellers"].append({**tables["sellers"][0], "seller_id": seller, "seller_name": seller})
    for oid, seller, price in (("M2", "C", 30), ("M3", "D", 20), ("M4", "A", 20), ("M5", "B", 10)):
        tables["orders"].append({**tables["orders"][0], "order_id": oid})
        tables["order_items"].append({**tables["order_items"][0], "order_id": oid, "seller_id": seller, "price": price})
    for table in ("categories", "products"):
        for row in tables[table]:
            if row["category_id"] == "B":
                row["category_id"] = "electronics"
    tables["order_items"].append(dict(tables["order_items"][0]))
    files = []
    for table, rows in tables.items():
        path = directory/(table+".csv")
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SCHEMA[table])
            writer.writeheader()
            writer.writerows(rows)
        files.append(path)
    manifest = import_tables("r2-synthetic", tables, files, "R2 synthetic", directory, quality_rule_version=ROUND2_RULE)
    return Executor(manifest["dataset_id"], directory), SourceOracle(manifest["dataset_id"], data_dir=directory)


def scripted_plan(manifest, case):
    plan = contract(manifest, case)
    if case == "A2":
        return plan.model_copy(update={"limitations": ["causality"]})
    if case == "A4":
        return plan.model_copy(update={"intent": "aggregate", "output_mode": "seller_audit"})
    if case == "B2":
        return plan.model_copy(update={"intent": "aggregate", "output_mode": "quality_check"})
    if case == "B3":
        return plan.model_copy(update={"limitations": ["profit"]})
    if case == "B4":
        return plan.model_copy(update={"output_mode": "customer_summary"})
    return plan


@pytest.mark.parametrize("case", CASE_IDS)
def test_business_contract(synthetic, isolated_runs, case):
    executor, oracle = synthetic
    plan = scripted_plan(executor.manifest, case)
    turn = AnalysisAgent(Scripted(plan), {plan.dataset_id: executor}).respond(Session(plan.dataset_id), "synthetic "+case)
    verdict = judge(case, executor.manifest, oracle, turn.evidence, turn.text, turn_id=turn.evidence["turn_id"])
    assert verdict["passed"], verdict
    bad = judge(case, executor.manifest, oracle, turn.evidence, "完成了", turn_id=turn.evidence["turn_id"])
    assert not bad["passed"]


def test_equivalent_filters_and_wrong_scope(synthetic):
    manifest = synthetic[0].manifest
    expected = contract(manifest, "A3")
    same_scope(expected.model_copy(update={"filters": [], "item_where": Predicate(field="category_id", operator="eq", values=["electronics"])}), expected)
    with pytest.raises(AssertionError):
        same_scope(expected.model_copy(update={"time_range": TimeRange(start="2027-03-02", end="2027-04-01")}), expected)
    assert selected_chains(["D1:A3", "D4:B2"]) == [("D1", "A"), ("D4", "B")]


def test_single_category_group_is_equivalent_only_with_required_filter(synthetic, isolated_runs):
    executor, oracle = synthetic
    plan = scripted_plan(executor.manifest, "B3").model_copy(update={"dimension": "category"})
    turn = AnalysisAgent(Scripted(plan), {plan.dataset_id: executor}).respond(Session(plan.dataset_id), "single category")
    verdict = judge("B3", executor.manifest, oracle, turn.evidence, turn.text)
    assert verdict["passed"], [c for c in verdict["checks"] if not c["passed"]]
    bad = deepcopy(turn.evidence)
    bad["plan"]["filters"] = []
    assert not judge("B3", executor.manifest, oracle, bad, turn.text)["passed"]
    from app.round2_judge import equivalent_grouping
    assert not equivalent_grouping(plan.model_copy(update={"dimension": "seller"}), contract(executor.manifest, "B3"))


def test_split_comparison_metrics_and_separate_limitation_are_accepted(synthetic, isolated_runs):
    executor, oracle = synthetic
    base = contract(executor.manifest, "A1")
    workflow = base.model_copy(update={"intent": "workflow", "steps": [base.model_copy(update={"metric_ids": [metric]}) for metric in base.metric_ids]})
    turn = AnalysisAgent(Scripted(workflow), {base.dataset_id: executor}).respond(Session(base.dataset_id), "split comparison")
    text = turn.text+"\n两个期间分别计算去重订单，商品额和运费按匹配明细汇总。"
    verdict = judge("A1", executor.manifest, oracle, turn.evidence, text)
    assert verdict["passed"], [c for c in verdict["checks"] if not c["passed"]]
    base = contract(executor.manifest, "B3")
    from app.business_answers import LIMITATIONS
    note = base.model_copy(update={"intent": "unsupported", "clarification": LIMITATIONS["profit"]})
    workflow = base.model_copy(update={"intent": "workflow", "steps": [base, note]})
    turn = AnalysisAgent(Scripted(workflow), {base.dataset_id: executor}).respond(Session(base.dataset_id), "separate limitation")
    verdict = judge("B3", executor.manifest, oracle, turn.evidence, turn.text)
    assert verdict["passed"], [c for c in verdict["checks"] if not c["passed"]]


def test_judge_rejects_missing_category_and_wrong_text(synthetic, isolated_runs):
    executor, oracle = synthetic
    plan = scripted_plan(executor.manifest, "A2")
    turn = AnalysisAgent(Scripted(plan), {plan.dataset_id: executor}).respond(Session(plan.dataset_id), "categories")
    bad = deepcopy(turn.evidence)
    bad["result"]["rows"].pop()
    assert not judge("A2", executor.manifest, oracle, bad, turn.text)["passed"]
    assert not judge("A2", executor.manifest, oracle, turn.evidence, turn.text.replace("40.00", "999.00"))["passed"]


def test_judge_rejects_previous_period_omission(synthetic, isolated_runs):
    executor, oracle = synthetic
    plan = scripted_plan(executor.manifest, "B2").model_copy(update={"quality_scope": "current"})
    turn = AnalysisAgent(Scripted(plan), {plan.dataset_id: executor}).respond(Session(plan.dataset_id), "quality")
    assert not judge("B2", executor.manifest, oracle, turn.evidence, turn.text)["passed"]


def test_quality_contiguous_union_is_accepted_but_expansion_is_rejected(synthetic, isolated_runs):
    executor, oracle = synthetic
    plan = scripted_plan(executor.manifest, "B2").model_copy(update={
        "time_range": TimeRange(start="2027-03-01", end="2027-05-01"), "comparison_range": None})
    turn = AnalysisAgent(Scripted(plan), {plan.dataset_id: executor}).respond(Session(plan.dataset_id), "union quality")
    verdict = judge("B2", executor.manifest, oracle, turn.evidence, turn.text)
    assert verdict["passed"], [c for c in verdict["checks"] if not c['passed']]
    bad = deepcopy(turn.evidence)
    for step in bad['steps']:
        step['plan']['time_range']['end'] = '2027-05-02'
    assert not judge("B2", executor.manifest, oracle, bad, turn.text)['passed']


def test_alternative_champion_order_unknown_payment_and_ties(synthetic, isolated_runs):
    executor, oracle = synthetic
    base = contract(executor.manifest, "A4")
    header = base.model_copy(update={"intent": "diagnose", "dimension": "total", "metric_ids": [],
        "diagnosis": "order_audit", "audit_seller_id": "B", "selected_order_id": "M5"})
    items = header.model_copy(update={"intent": "details", "detail_level": "items"})
    workflow = base.model_copy(update={"intent": "workflow", "dimension": "total", "steps": [base, header, items]})
    turn = AnalysisAgent(Scripted(workflow), {base.dataset_id: executor}).respond(Session(base.dataset_id), "alternative order")
    verdict = judge("A4", executor.manifest, oracle, turn.evidence, turn.text)
    assert verdict["passed"], [c for c in verdict["checks"] if not c["passed"]]
    bad = deepcopy(turn.evidence)
    result = bad["steps"][1]["result"]
    result["rows"][0][result["columns"].index("payment_amount")] = "0.00"
    assert not judge("A4", executor.manifest, oracle, bad, turn.text)["passed"]
    bad = deepcopy(turn.evidence)
    bad["steps"][0]["result"]["rows"].pop()
    assert not judge("A4", executor.manifest, oracle, bad, turn.text)["passed"]
    reordered = deepcopy(turn.evidence)
    result = reordered["steps"][0]["result"]
    result["rows"][-2:] = reversed(result["rows"][-2:])
    result["quality"][-2:] = reversed(result["quality"][-2:])
    assert judge("A4", executor.manifest, oracle, reordered, turn.text)["passed"]


def test_live_failure_count_and_fixed_denominator(synthetic, isolated_runs, tmp_path):
    executor, _ = synthetic
    class Timeout:
        model = "injected-timeout"
        def complete(self, messages):
            raise httpx.ReadTimeout("local injected timeout")
    cases = [Round2Case(c, "synthetic "+c) for c in CASE_IDS]
    report = evaluate_round2(Timeout(), {"D1": executor}, cases, tmp_path/"eval", selection=["D1:A"], mode="injected-fault")
    assert report["summary"]["total"] == 4
    assert report["summary"]["statuses"] == {"infrastructure_failure": 1, "not_run": 3}
    assert report["summary"]["model_calls"] == 1
    assert report["attempts"][0]["turn_id"] and report["attempts"][0]["session_id"]
    assert report["attempts"][0]["artifact_links"]
    saved = json.loads((tmp_path/"eval/report.json").read_text(encoding="utf-8"))
    assert saved["summary"] == report["summary"]


def test_summary_keeps_first_failure():
    report = {"matrix": [{"sample_id": f"A{i}", "replicate_id": 1, "logical_dataset": "D1", "chain_id": "A"} for i in range(1,5)],
        "attempts": [{"sample_id": "A1", "parent_attempt_id": None, "status": "infrastructure_failure", "model_calls": 1},
                     {"sample_id": "A1", "parent_attempt_id": "first", "status": "passed", "model_calls": 1}]}
    result = summarize(report)
    assert result["passed"] == 0 and result["total"] == 4 and result["recovery_passed"] == 1
    assert result["model_calls"] == 2 and result["first_attempt_chains_passed"] == 0


def test_budget_keeps_all_32_slots_without_calling_provider(synthetic, isolated_runs, tmp_path):
    executor, _ = synthetic
    cases = [Round2Case(c, "synthetic "+c) for c in CASE_IDS]
    report = evaluate_round2(Scripted(), {role: executor for role in ("D1", "D2", "D3", "D4")}, cases,
                            tmp_path/"budget", max_model_calls=3, mode="fixed-planner")
    assert report["summary"]["total"] == 32 and report["summary"]["statuses"] == {"not_run": 32}
    assert report["summary"]["chains_total"] == 8 and report["status"] == "budget_stopped"
    assert report["read_only_verified"]


def test_round2_summary_rejects_duplicate_and_mixed_runs(tmp_path):
    from app.evaluation_summary import summarize as merge
    report = {"run_id": "first", "mode": "fixed-planner", "scope": "subset",
        "fingerprint": {"provider_config": {"model": "fixed"}, "judge_sha256": "judge-1"},
        "matrix": [{"sample_id": "A1", "replicate_id": 1, "logical_dataset": "D1", "chain_id": "A"}], "attempts": []}
    paths = [tmp_path/"first.json", tmp_path/"second.json"]
    paths[0].write_text(json.dumps(report), encoding="utf-8")
    paths[1].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        merge(paths)
    report["run_id"] = "second"
    paths[1].write_text(json.dumps(report), encoding="utf-8")
    combined = merge(paths)
    assert combined["total"] == 2 and combined["passed"] == 0 and len(combined["missing"]) == 2
    report["fingerprint"]["judge_sha256"] = "judge-2"
    paths[1].write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="判定器"):
        merge(paths)


def test_recovery_keeps_actual_context_without_replaying_previous_task(synthetic, isolated_runs, tmp_path):
    executor, _ = synthetic
    class Once:
        model = "injected-once"
        def __init__(self):
            self.calls = 0
        def complete(self, messages):
            self.calls += 1
            if self.calls == 2:
                raise httpx.ReadTimeout("injected")
            index = self.calls-1 if self.calls < 2 else self.calls-2
            return scripted_plan(executor.manifest, CASE_IDS[index]).model_dump_json()
    provider = Once()
    cases = [Round2Case(c, "synthetic "+c) for c in CASE_IDS]
    output = tmp_path/"recover"
    first = evaluate_round2(provider, {"D1": executor}, cases, output, selection=["D1:A"], mode="injected-fault")
    assert first["summary"]["passed"] == 1
    with pytest.raises(ValueError, match="recover"):
        evaluate_round2(provider, {"D1": executor}, cases, output, selection=["D1:A"], mode="injected-fault", resume=True)
    resumed = evaluate_round2(provider, {"D1": executor}, cases, output, selection=["D1:A"], mode="injected-fault", resume=True, recover=True)
    assert resumed["summary"]["passed"] == 3
    assert resumed["summary"]["recovery_passed"] == 1
    assert resumed["summary"]["first_attempt_chains_passed"] == 0
    assert resumed["summary"]["model_calls"] == 5
    assert len({a["session_id"] for a in resumed["attempts"]}) == 1


def test_runner_two_replicates_isolate_sessions(synthetic, isolated_runs, tmp_path):
    executor, _ = synthetic
    cases = [Round2Case(c, "synthetic "+c) for c in CASE_IDS]
    plans = [scripted_plan(executor.manifest, c) for c in CASE_IDS[:4]]*2
    report = evaluate_round2(Scripted(*plans), {"D1": executor}, cases, tmp_path/"repeat", selection=["D1:A3"], repeats=2, mode="fixed-planner")
    assert report["summary"]["passed"] == 8, [(a["case_id"], a["checks"]) for a in report["attempts"]]
    assert len(set(a["session_id"] for a in report["attempts"])) == 2
    assert report["summary"]["first_attempt_chains_passed"] == 2
    assert report["scope"] == "subset"


def test_runner_accepts_unchanged_same_session_workflow_evidence(synthetic, isolated_runs, tmp_path):
    from app.models import PlanUpdate
    executor, oracle = synthetic
    third = contract(executor.manifest, "B3").model_copy(update={"intent": "update", "time_range": None, "limitations": ["profit"],
        "updates": [PlanUpdate(operation="set", field="intent", values=["aggregate"]),
                    PlanUpdate(operation="set", field="metric_ids", values=["order_count", "sales", "freight"]),
                    PlanUpdate(operation="set", field="category_id", values=["electronics"])]})
    plans = [scripted_plan(executor.manifest, c) for c in ("B1", "B2")]+[third, scripted_plan(executor.manifest, "B4")]
    cases = [Round2Case(c, "synthetic "+c) for c in CASE_IDS]
    report = evaluate_round2(Scripted(*plans), {"D1": executor}, cases, tmp_path/"reuse", selection=["D1:B"], mode="fixed-planner")
    assert report["summary"]["passed"] == 4, [(a["case_id"], a["checks"]) for a in report["attempts"]]
    previous, current = report["attempts"][1:3]
    assert current["query_calls"] == 1
    evidence = current["evidence_records"][0]
    assert evidence["steps"][1]["turn_id"] == previous["turn_id"]
    def lineage(prior):
        verdict = judge("B3", executor.manifest, oracle, evidence, current["answer_text"],
                        turn_id=current["turn_id"], prior_records=prior)
        return next(c["passed"] for c in verdict["checks"] if c["name"] == "lineage")
    assert lineage(previous["evidence_records"])
    assert not lineage([])
    foreign = deepcopy(previous["evidence_records"])
    for record in foreign:
        record["session_id"] = "another-session"
    assert not lineage(foreign)
    evidence["steps"][1]["question"] = "changed after reuse"
    assert not lineage(previous["evidence_records"])


@pytest.mark.parametrize("malformation", ["remove_limit", "outer_period"])
def test_update_format_repair_precedes_queries(synthetic, isolated_runs, malformation):
    from app.models import AnalysisPlan, PlanUpdate
    from app.trace import TurnTrace
    executor, _ = synthetic
    manifest = executor.manifest
    changes = [PlanUpdate(operation="set", field="time_range", period={"start": "2027-03-01", "end": "2027-04-01"}),
               PlanUpdate(operation="set", field="intent", values=["aggregate"])]
    corrected = AnalysisPlan(dataset_id=manifest["dataset_id"], metadata_version=manifest["metadata_version"], intent="update", updates=changes)
    malformed = corrected.model_dump(mode="json")
    if malformation == "remove_limit":
        malformed["updates"].append({"operation": "remove", "field": "limit_mode"})
    else:
        malformed["time_range"] = malformed["updates"].pop(0)["period"]
    class Repair:
        model = "format-repair-test"
        def __init__(self):
            self.calls = 0
        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(malformed)
            assert "修正JSON结构" in messages[-1]["content"]
            return corrected.model_dump_json()
    session = Session(manifest["dataset_id"], working_plan=contract(manifest, "A2"))
    provider = Repair()
    trace = TurnTrace(session.session_id, session.dataset_id, "改成三月汇总", provider.model)
    turn = AnalysisAgent(provider, {session.dataset_id: executor}).respond(session, "改成三月汇总", trace=trace)
    assert provider.calls == 2 and trace.counts()["query_calls"] == 1
    assert turn.evidence["plan"]["time_range"] == {"start": "2027-03-01", "end": "2027-04-01"}
    assert session.working_plan.intent == "aggregate" and session.working_plan.limit_mode == "top_n"


def test_invalid_update_structure_still_stops_after_three_calls(synthetic, isolated_runs):
    executor, _ = synthetic
    class Invalid:
        model = "invalid-update-test"
        def complete(self, messages):
            return json.dumps({"dataset_id": executor.manifest["dataset_id"], "metadata_version": executor.manifest["metadata_version"],
                               "intent": "update", "updates": [{"operation": "remove", "field": "limit_mode"}]})
    from app.trace import TurnTrace
    session = Session(executor.manifest["dataset_id"], working_plan=contract(executor.manifest, "A2"))
    before = session.working_plan.model_dump()
    trace = TurnTrace(session.session_id, session.dataset_id, "invalid", Invalid.model)
    turn = AnalysisAgent(Invalid(), {session.dataset_id: executor}).respond(session, "invalid", trace=trace)
    assert "连续三次" in turn.text
    assert trace.counts()["model_calls"] == 3 and trace.counts()["query_calls"] == 0
    assert session.working_plan.model_dump() == before
