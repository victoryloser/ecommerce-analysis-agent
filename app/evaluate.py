"""真实模型验收：原题、改写、参数变体、多轮与边界分别记录。"""
import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.config import DATASETS, RUNS_DIR
from app.evaluation_cases import assert_plan, cases_for
from app.executor import Executor
from app.models import AnalysisPlan, EntityFilter, QueryResult
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session


def check_workflow_evidence(evidence, expected, oracle):
    assert len(evidence["tasks"]) == len(expected.steps), "任务状态不完整"
    for index, step in enumerate(expected.steps):
        task, record = evidence["tasks"][index], evidence["steps"][index]
        if step.intent in {"clarify", "unsupported"}:
            assert task["status"] == ("needs_input" if step.intent == "clarify" else "unsupported")
            assert_plan(AnalysisPlan.model_validate(record["plan"]), step)
            assert record["result"] is None
            continue
        if step.source_step:
            upstream = evidence["tasks"][step.source_step-1]
            if upstream["status"] != "completed":
                assert task["status"] == "pending", "依赖未完成却执行了下游"
                continue
            result = evidence["steps"][step.source_step-1]["result"]
            if not result["rows"] or step.source_row >= len(result["rows"]):
                assert task["status"] == "needs_input"
                continue
            row = dict(zip(result["columns"], result["rows"][step.source_row]))
            source_plan = expected.steps[step.source_step-1]
            ranking = "rank_no" if source_plan.intent == "rank" else source_plan.metric_ids[0]+"_change" if source_plan.intent == "contribution" else None
            metric = source_plan.metric_ids[0] if source_plan.metric_ids else None
            relevant = {metric, f"{metric}_current", f"{metric}_previous"}
            incomplete = any(q and q["completeness"] != "complete" for values in result.get("quality", []) for name, q in values.items() if name in relevant)
            tied = ranking and (row[ranking] is None or sum(values[result["columns"].index(ranking)] == row[ranking] for values in result["rows"]) > 1)
            if incomplete or tied:
                assert task["status"] == "needs_input", "未知或并列来源不应静默绑定单个实体"
                continue
            key = row["order_id"] if step.bind_field == "order_id" else row["entity_id"]
            updates = {"source_step": None, "bind_field": None, "source_row": 0}
            if step.bind_field == "order_id":
                updates["selected_order_id"] = key
            else:
                updates["filters"] = [f for f in step.filters if f.field != step.bind_field] + [EntityFilter(field=step.bind_field, values=[key])]
            step = step.model_copy(update=updates)
        assert task["status"] == "completed", f"步骤{index+1}未完成：{task['reason']}"
        actual = AnalysisPlan.model_validate(record["plan"])
        assert_plan(actual, step)
        if step.intent == "explain":
            assert record["result"] is None
        else:
            result = QueryResult.model_validate(record["result"])
            check_rows(result, oracle.evaluate(step))
            for values, qualities in zip(result.rows, result.quality):
                row = dict(zip(result.columns, values))
                for metric, quality in qualities.items():
                    if quality and quality["completeness"] != "complete":
                        assert row[metric] is None, f"步骤{index+1}将不完整的{metric}输出为完整值"


def evaluate(provider, executor, suite: str, selected: list[str]) -> dict:
    dataset = executor.manifest["dataset_id"]
    oracle = SourceOracle(dataset, data_dir=executor.path.parent)
    agent = AnalysisAgent(provider, {dataset: executor})
    cases = cases_for(executor.manifest, suite)
    if selected:
        groups = {c.conversation for c in cases if c.case_id in selected and c.conversation}
        cases = [c for c in cases if c.case_id in selected or (c.conversation and c.conversation in groups)]
        if not cases:
            raise ValueError("未匹配到验收用例")
    conversations = {}
    records = []
    path = RUNS_DIR / f"live-evaluation-{dataset}-{uuid4().hex}.json"
    report = {"mode": "live-model", "suite": suite, "selection": selected,
              "replicate_id": uuid4().hex,
              "model": provider.model, "dataset_id": dataset,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "database_sha256": executor.manifest["database_sha256"], "cases": records}
    report["metadata_sha256"] = executor.manifest["metadata_sha256"]
    from app.snapshots import implementation_hash
    report["implementation_sha256"] = implementation_hash()
    RUNS_DIR.mkdir(exist_ok=True)
    for case in cases:
        session = conversations.setdefault(case.conversation, Session(dataset)) if case.conversation else Session(dataset)
        started = time.perf_counter()
        failure = None
        evidence = {}
        try:
            turn = agent.respond(session, case.question, practice_context=case.practice)
            evidence = turn.evidence
            if case.expected_plan is None:
                assert session.last_successful_plan is None and session.pending_plan is None and not session.history, "重置没有清空状态"
            else:
                assert "plan" in evidence, turn.text
                actual = AnalysisPlan.model_validate(evidence["plan"])
                if case.expected_status == "out_of_coverage" and actual.intent == "unsupported":
                    # 数据不足可在规划阶段说明，也可在执行入口按覆盖期终止。
                    assert actual.clarification and evidence["result"] is None, "超范围应明确说明并停止统计"
                elif case.expected_plan.explanation_topic == "causality" and actual.intent == "unsupported":
                    reason = actual.clarification
                    assert reason and "促销" in reason and any(word in reason for word in ("缺少", "不足", "没有", "无法")), "应说明缺少促销归因证据"
                    assert evidence["result"] is None, "证据不足不能伪造数值查询"
                elif actual.intent in {"explain", "clarify", "unsupported"}:
                    assert_plan(actual, case.expected_plan)
                    assert evidence["result"] is None, "解释或澄清不应查询"
                elif actual.intent == "workflow":
                    assert_plan(actual, case.expected_plan)
                    check_workflow_evidence(evidence, case.expected_plan, oracle)
                else:
                    assert_plan(actual, case.expected_plan)
                    result = QueryResult.model_validate(evidence["result"])
                    assert result.status == case.expected_status, f"状态：{result.status} != {case.expected_status}"
                    if result.status == "ok":
                        expected = case.expected_plan.model_copy(update={"metric_ids": actual.metric_ids})
                        check_rows(result, oracle.evaluate(expected))
                    if case.case_id == "M02":
                        assert evidence["planning_calls"] == 0, "明确年份应由宿主补槽而非重新请求模型"
        except (AssertionError, ValueError, httpx.HTTPError) as error:
            # 验收失败计入完整分母；不重试、不切模型、不把失败改成通过。
            failure = f"{type(error).__name__}: {str(error) or '业务结果不一致'}"
        records.append({"case_id": case.case_id, "category": case.category, "question": case.question,
                        "replicate_id": report["replicate_id"],
                        "conversation": case.conversation, "passed": failure is None, "failure": failure,
                        "elapsed_ms": round((time.perf_counter()-started)*1000),
                        "planning_calls": evidence.get("planning_calls", 0), "evidence_id": evidence.get("record_id"),
                        "expected_plan": case.expected_plan.model_dump(mode="json") if case.expected_plan else None})
        report.update(passed=sum(r["passed"] for r in records), total=len(cases), completed=len(records),
                      categories={category: {"passed": sum(r["passed"] for r in records if r["category"] == category), "total": count}
                                  for category, count in Counter(c.category for c in cases).items()})
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(dataset, case.case_id, "PASS" if failure is None else f"FAIL: {failure}", flush=True)
    print(path, flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=[*DATASETS, "all"], default="csv_small")
    parser.add_argument("--suite", choices=["original", "extended", "all"], default="original")
    parser.add_argument("--case", action="append", default=[], help="仅运行指定用例；多轮用例会保留整段对话")
    args = parser.parse_args()
    provider = ChatCompletionsProvider()
    from app.snapshots import resolve_dataset
    reports = [evaluate(provider, Executor(resolve_dataset(dataset)), args.suite, args.case)
               for dataset in DATASETS if args.dataset == "all" or dataset == args.dataset]
    if any(r["passed"] != r["total"] for r in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
