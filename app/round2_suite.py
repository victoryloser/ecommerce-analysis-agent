"""Public round-two question loader and evaluator-only business contracts."""
from dataclasses import dataclass
from pathlib import Path
import re

from app.models import AnalysisPlan, TimeRange

SUITE_VERSION = "round2-v1.1-contract-1"
ROLES = ("D1", "D2", "D3", "D4")
CASE_IDS = tuple(f"{group}{i}" for group in "AB" for i in range(1, 5))


@dataclass(frozen=True)
class Round2Case:
    case_id: str
    question: str


def load_cases(path: Path):
    questions = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\|\s*([AB][1-4])\s*\|([^|]+)\|", line)
        if match:
            if match[1] in questions:
                raise ValueError(f"重复题号：{match[1]}")
            questions[match[1]] = match[2].strip()
    if set(questions) != set(CASE_IDS):
        raise ValueError("题面必须包含 A1–A4、B1–B4")
    return [Round2Case(key, questions[key]) for key in CASE_IDS]


def contract(manifest, case_id):
    plan = AnalysisPlan(dataset_id=manifest["dataset_id"], metadata_version=manifest["metadata_version"],
        intent="aggregate", metric_ids=["sales", "freight", "order_count"],
        time_range=TimeRange(start="2027-04-01", end="2027-05-01"))
    if case_id in {"A1", "B1", "A2", "B2"}:
        plan = plan.model_copy(update={"intent": "compare", "comparison_range": TimeRange(start="2027-03-01", end="2027-04-01")})
    if case_id == "A2":
        plan = plan.model_copy(update={"intent": "contribution", "dimension": "category", "metric_ids": ["sales"], "limit_mode": "all"})
    if case_id in {"A3", "A4"}:
        plan = plan.model_copy(update={"time_range": TimeRange(start="2027-03-01", end="2027-04-01")})
    if case_id in {"A3", "B3"}:
        from app.models import EntityFilter
        plan = plan.model_copy(update={"filters": [EntityFilter(field="category_id", values=["electronics"])]})
    if case_id == "A4":
        plan = plan.model_copy(update={"intent": "rank", "dimension": "seller", "metric_ids": ["sales", "order_count"], "top_n": 3})
    if case_id == "B2":
        plan = plan.model_copy(update={"intent": "diagnose", "diagnosis": "quality_rows", "metric_ids": []})
    if case_id == "B4":
        plan = plan.model_copy(update={"time_range": TimeRange(**{k: manifest["coverage"][k] for k in ("start", "end")}),
                                       "metric_ids": ["order_count", "repeat_customers"]})
    return plan


def selected_chains(selection):
    if not selection:
        return [(role, group) for role in ROLES for group in "AB"]
    chains = set()
    for item in selection:
        role, case = item.split(":")
        if role not in ROLES or case not in (*CASE_IDS, "A", "B"):
            raise ValueError(f"无效用例：{item}；使用 D1:A 或 D4:B3")
        chains.add((role, case[0]))
    return sorted(chains)
