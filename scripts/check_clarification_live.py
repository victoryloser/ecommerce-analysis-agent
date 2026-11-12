"""新意图缺年份与工作流补槽的定向模型验收；会消耗当前配置模型配额。"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import DATASETS, ROOT, RUNS_DIR
from app.demo import practice_plans
from app.executor import Executor
from app.models import AnalysisPlan
from app.oracle import SourceOracle
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session
from check_extended_live import check_contribution


def check_details(oracle, evidence):
    plan, result = AnalysisPlan.model_validate(evidence["plan"]), evidence["result"]
    assert plan.intent == "details" and plan.detail_level == "orders" and not plan.comparison_range
    assert str(plan.time_range.start) == "2025-04-01" and str(plan.time_range.end) == "2025-05-01"
    categories = {v for f in plan.filters if f.field == "category_id" for v in f.values}
    expected = sorted(o["order_id"] for o in oracle.orders if o["order_status"] == "delivered"
        and "2025-04-01" <= str(o["purchase_at"])[:10] < "2025-05-01"
        and (not categories or any(oracle.products[i["product_id"]]["category_id"] in categories for i in oracle.items[o["order_id"]])))
    assert result["status"] in {"ok", "empty"}
    assert [row[result["columns"].index("order_id")] for row in result["rows"]] == expected[:500]


def check_reconciliation(oracle, evidence):
    plan, result = AnalysisPlan.model_validate(evidence["plan"]), evidence["result"]
    assert plan.intent == "diagnose" and plan.diagnosis == "reconciliation"
    assert str(plan.time_range.start) == "2025-04-01" and str(plan.time_range.end) == "2025-05-01"
    assert plan.diagnostic_threshold == 0.01 and plan.order_statuses == ["delivered"]
    expected = set()
    for order in oracle.orders:
        if order["order_status"] == "delivered" and "2025-04-01" <= str(order["purchase_at"])[:10] < "2025-05-01":
            oid = order["order_id"]
            amount = sum((Decimal(str(i["price"]))+Decimal(str(i["freight_value"])) for i in oracle.items[oid]), Decimal(0))
            if abs(oracle.payments[oid]-amount) > Decimal(".01"):
                expected.add(oid)
    assert result["status"] in {"ok", "empty"}
    assert {row[result["columns"].index("order_id")] for row in result["rows"]} == expected


def main():
    provider = ChatCompletionsProvider()
    report = {"mode": "live-model-clarification", "model": provider.model, "created_at": datetime.now(timezone.utc).isoformat(),
        "implementation_sha256": hashlib.sha256(b"".join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/"app").glob("*.py")))).hexdigest(),
        "total": 8, "passed": 0, "status": "running", "cases": []}
    path = RUNS_DIR/f"clarification-live-{uuid4().hex}.json"
    for dataset in DATASETS:
        executor, oracle = Executor(dataset), SourceOracle(dataset)
        agent = AnalysisAgent(provider, {dataset: executor})
        contribution = practice_plans(executor.manifest)["Q03"].model_copy(update={"intent": "contribution", "dimension": "category"})
        questions = {
            "details": "列出4月下单且已送达的订单明细。年份未指定，请先向我澄清年份。",
            "diagnose": "对4月下单且已送达订单逐单对账，支付额与商品金额加运费差额超过0.01元的有哪些？年份未指定，请先澄清年份。",
            "contribution": "计算4月相对3月各品类对已送达商品销售额净增长的贡献，年份未指定，请先澄清年份。",
            "workflow": "先计算4月相对3月各品类对已送达商品销售额净增长的贡献，再列出贡献最高品类4月下单且已送达的订单明细。年份未指定，请先问我年份，并保留两个步骤及它们的依赖关系。",
        }
        for name, question in questions.items():
            session, evidence, failure = Session(dataset), {}, None
            try:
                turn = agent.respond(session, question)
                evidence = turn.evidence
                assert evidence["type"] == "clarify", turn.text
                assert session.pending_plan.pending_intent == name
                assert session.last_result is None and not session.registered_plans
                initial_id = evidence["record_id"]
                replies = []
                # 工作流最多4步，可能分别询问年份；每次回答只补当前待澄清步骤。
                for _ in range(4):
                    turn = agent.respond(session, "2025年")
                    evidence = turn.evidence
                    assert evidence["planning_calls"] == 0
                    replies.append(evidence["record_id"])
                    if session.pending_plan is None:
                        break
                assert session.pending_plan is None
                if name == "details":
                    check_details(oracle, evidence)
                elif name == "diagnose":
                    check_reconciliation(oracle, evidence)
                elif name == "contribution":
                    check_contribution(oracle, evidence, contribution)
                else:
                    assert evidence["type"] == "workflow" and len(evidence["steps"]) == 2
                    source, details = evidence["steps"]
                    check_contribution(oracle, source, contribution)
                    assert evidence["plan"]["steps"][1]["source_step"] == 1
                    key = source["result"]["rows"][0][0]
                    assert details["plan"]["filters"] == [{"field": "category_id", "values": [key]}]
                    check_details(oracle, details)
                report["cases"].append({"dataset_id": dataset, "case": name, "passed": True,
                    "initial_evidence_id": initial_id, "reply_evidence_ids": replies, "evidence_id": evidence["record_id"]})
            except Exception as error:
                failure = str(error) if isinstance(error, AssertionError) else type(error).__name__
                report["cases"].append({"dataset_id": dataset, "case": name, "passed": False, "failure": failure,
                    "evidence_id": evidence.get("record_id")})
                raise
            finally:
                report.update(passed=sum(c["passed"] for c in report["cases"]), status="failed" if failure else "running")
                path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(dataset, name, "FAIL" if failure else "PASS", flush=True)
    report["status"] = "passed"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path, flush=True)


if __name__ == "__main__":
    main()
