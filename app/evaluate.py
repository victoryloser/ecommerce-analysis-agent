"""真实模型验收：原题、改写、参数变体、多轮与边界分别记录。"""
import argparse
import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.config import DATASETS, ROOT, RUNS_DIR
from app.evaluation_cases import assert_plan, cases_for
from app.executor import Executor
from app.models import AnalysisPlan, QueryResult
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session


def evaluate(provider, executor, suite: str, selected: list[str]) -> dict:
    dataset = executor.manifest["dataset_id"]
    oracle = SourceOracle(dataset)
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
              "model": provider.model, "dataset_id": dataset,
              "created_at": datetime.now(timezone.utc).isoformat(),
              "database_sha256": executor.manifest["database_sha256"], "cases": records}
    report["metadata_sha256"] = executor.manifest["metadata_sha256"]
    report["implementation_sha256"] = hashlib.sha256(b"".join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/"app").glob("*.py")))).hexdigest()
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
                else:
                    assert_plan(actual, case.expected_plan)
                    result = QueryResult.model_validate(evidence["result"])
                    assert result.status == case.expected_status, f"状态：{result.status} != {case.expected_status}"
                    if result.status == "ok":
                        expected = case.expected_plan.model_copy(update={"metric_ids": actual.metric_ids})
                        check_rows(result, oracle.evaluate(expected))
                    if case.case_id == "M02":
                        assert evidence["planning_calls"] == 0, "明确年份应由宿主补槽而非重新请求模型"
        except (AssertionError, ValidationError, httpx.HTTPError) as error:
            # 验收失败计入完整分母；不重试、不切模型、不把失败改成通过。
            failure = f"{type(error).__name__}: {str(error) or '业务结果不一致'}"
        records.append({"case_id": case.case_id, "category": case.category, "question": case.question,
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
    reports = [evaluate(provider, Executor(dataset), args.suite, args.case)
               for dataset in DATASETS if args.dataset == "all" or dataset == args.dataset]
    if any(r["passed"] != r["total"] for r in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
