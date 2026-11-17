"""显式的离线计划演示，不调用模型，也不冒充自然语言能力测试。"""
import argparse

from app.answers import answer, explain
from app.config import DATASETS
from app.evidence import write_evidence
from app.executor import Executor
from app.models import AnalysisPlan
from app.semantic import validate_plan
from app.session import Session


def practice_plans(manifest: dict) -> dict[str, AnalysisPlan]:
    common = {"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"], "intent": "aggregate",
              "time_range": {k: manifest["coverage"][k] for k in ["start", "end"]}}
    specifications = {
        "Q01": {"metric_ids": ["order_count"]},
        "Q02": {"metric_ids": ["sales"]},
        "Q03": {"intent": "compare", "metric_ids": ["sales"], "time_range": {"start": "2025-04-01", "end": "2025-05-01"}, "comparison_range": {"start": "2025-03-01", "end": "2025-04-01"}},
        "Q04": {"intent": "rank", "metric_ids": ["sales"], "dimension": "category", "time_range": {"start": "2025-04-01", "end": "2025-05-01"}, "top_n": 1},
        "Q05": {"intent": "rank", "metric_ids": ["delay_rate"], "dimension": "seller", "time_range": {"start": "2025-05-01", "end": "2025-06-01"}, "min_group_count": manifest["practice"]["min_seller_orders"], "top_n": 1},
        "Q06": {"metric_ids": ["avg_review"], "dimension": "delivery_status"},
        "Q07": {"metric_ids": ["repeat_customers"]},
        "Q08": {"intent": "explain", "metric_ids": [], "time_range": None, "explanation_topic": "joins"},
    }
    return {qid: AnalysisPlan.model_validate(common | fields) for qid, fields in specifications.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    args = parser.parse_args()
    for dataset in DATASETS if args.dataset == "all" else [args.dataset]:
        from app.snapshots import resolve_dataset
        dataset = resolve_dataset(dataset)
        executor = Executor(dataset)
        session = Session(dataset)
        for question in executor.manifest["practice"]["questions"]:
            qid = question["question_id"]
            plan = practice_plans(executor.manifest)[qid]
            validate_plan(plan, executor.manifest)
            if plan.intent == "explain":
                text = explain(plan)
                record = write_evidence(session, executor.manifest, question["question"], plan, "offline-plan-demo", 0)
            else:
                result, compiled = executor.execute_plan(session, executor.register(session, plan))
                text = answer(plan, result, executor.manifest)
                record = write_evidence(session, executor.manifest, question["question"], plan, "offline-plan-demo", 0, result, compiled)
                if result.status not in {"ok", "empty"}:
                    raise RuntimeError(result.error)
            print(f"\n{dataset} {qid} {question['question']}\n{text}\n证据: {record['record_id']}")


if __name__ == "__main__":
    main()
