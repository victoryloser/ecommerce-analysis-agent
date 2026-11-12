"""会话、下钻与 SQL 模型集成测试；每项独立来源表核对，不重试、不切模型。"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import DATASETS, ROOT, RUNS_DIR
from app.demo import practice_plans
from app.executor import Executor
from app.models import QueryResult, TimeRange
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace


def check_shares(actual: QueryResult, expected: list[dict]) -> None:
    assert set(actual.columns) == {"entity_id", "sales", "share"}
    projected = actual.model_copy(update={"columns": ["entity_id", "sales"],
        "rows": [[dict(zip(actual.columns, row))[key] for key in ("entity_id", "sales")] for row in actual.rows]})
    check_rows(projected, [{key: row[key] for key in ("entity_id", "sales")} for row in expected])
    # 自定义探索SQL允许舍入；金额按分、比例按小数点后6位核对。
    for values, wanted in zip(actual.rows, expected):
        observed = dict(zip(actual.columns, values))["share"]
        assert abs(Decimal(str(observed)) - wanted["share"]) <= Decimal("0.0000005")


def main():
    provider = ChatCompletionsProvider()
    store = SessionStore()
    cases = []
    report = {"mode": "live-model-p1", "model": provider.model, "created_at": datetime.now(timezone.utc).isoformat(),
              "implementation_sha256": hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in sorted((ROOT / "app").glob("*.py")))).hexdigest(),
              "cases": cases, "status": "in_progress", "snapshots": {}}
    path = RUNS_DIR / f"p1-live-{uuid4().hex}.json"

    def record(dataset, stage, evidence):
        cases.append({"dataset_id": dataset, "stage": stage, "passed": True, "evidence_id": evidence["record_id"],
                      "session_id": evidence["session_id"]})
        report.update(passed=len(cases), total=10, completed=len(cases))
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(dataset, stage, "PASS", flush=True)

    for dataset in DATASETS:
        executor = Executor(dataset)
        report["snapshots"][dataset] = {key: executor.manifest[key] for key in ("database_sha256", "metadata_sha256", "schema_sha256")}
        oracle = SourceOracle(dataset)
        agent = AnalysisAgent(provider, {dataset: executor}, store)
        workspace = SQLWorkspace(agent.executors, store)
        session = Session(dataset)
        expected = practice_plans(executor.manifest)["Q02"].model_copy(update={"dimension": "month",
            "time_range": TimeRange(start="2025-01-01", end="2025-07-01")})
        first = agent.respond(session, "2025年上半年，按月统计商品销售额（不含运费），只统计已送达订单。")
        assert session.last_successful_plan == expected, first.text
        check_rows(session.last_result, oracle.evaluate(expected))
        record(dataset, "monthly-analysis", first.evidence)
        april_index = next(i for i, row in enumerate(session.last_result.rows) if row[0] == "2025-04")
        drill = agent.drill(session, april_index, "category")
        check_rows(session.last_result, oracle.evaluate(session.last_successful_plan))
        assert str(session.last_successful_plan.time_range.start) == "2025-04-01"
        record(dataset, "click-drill", drill.evidence)
        agent.back(session)
        draft = workspace.draft(session, "计算每个月商品销售额占这些月份合计的比例。share为0到1的比例，不要乘100；至少保留6位小数或不舍入。输出entity_id、sales、share，按entity_id排序。", "analysis_result", provider)
        result = workspace.execute(session, draft.sql, [], "analysis_result")
        rows = oracle.evaluate(expected)
        total = sum(row["sales"] for row in rows)
        expected_shares = [{"entity_id": row["entity_id"], "sales": row["sales"], "share": row["sales"] / total} for row in rows]
        check_shares(QueryResult.model_validate(result["result"]), expected_shares)
        record(dataset, "generated-sql-over-analysis", result)
        raw = workspace.draft(session,
            "按purchase_at的月份统计2025年1月1日至6月30日已送达订单的商品销售额，不含运费。"
            "输出entity_id（strftime格式%Y-%m）和sales两列，按entity_id排序。", "raw", provider)
        result = workspace.execute(session, raw.sql, [], "raw")
        check_rows(QueryResult.model_validate(result["result"]), [{"entity_id": row["entity_id"], "sales": row["sales"]} for row in rows])
        record(dataset, "generated-sql-over-raw", result)
        restored = SessionStore(store.path).load(session.session_id, executor.manifest)
        resumed = AnalysisAgent(provider, agent.executors, store).respond(restored, "只看4月，其余统计口径不变。")
        expected = expected.model_copy(update={"time_range": TimeRange(start="2025-04-01", end="2025-05-01")})
        assert restored.last_successful_plan == expected, resumed.text
        check_rows(restored.last_result, oracle.evaluate(expected))
        record(dataset, "restored-conversation-followup", resumed.evidence)
    report["status"] = "passed"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
