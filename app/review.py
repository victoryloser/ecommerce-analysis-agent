"""A saved review links to audited results and never changes the active analysis."""
import csv
import json
from decimal import Decimal
from pathlib import Path

from app.config import MAX_ROWS
from app.drilldown import selected_row_index
from app.evidence import read_evidence, write_artifact
from app.inspection_sources import retained_order_records
from app.models import AnalysisPlan, QueryResult
from app.orchestrator import AnalysisAgent
from app.semantic import PlanError
from app.session import Session
from app.session_store import snapshot_identity
from app.snapshot_comparison import fixed_plan
from app.trace import TurnTrace


METRICS = ["order_count", "sales", "freight"]
REVIEW_STEPS = (("overview_id", "两期汇总"), ("contribution_id", "品类贡献"))


def review_base(record):
    if record.get("type") != "query" or (record.get("result") or {}).get("status") not in {"ok", "empty"}:
        return None
    plan = AnalysisPlan.model_validate(record["plan"])
    if plan.intent == "compare" and plan.dimension == "total" and not plan.group_by:
        return plan
    return None


def _save(agent, session, payload):
    manifest = agent.executors[session.dataset_id].manifest
    title = "经营复盘"
    with TurnTrace(session.session_id, session.dataset_id, title, "review") as trace:
        card = write_artifact(session, manifest, "business_review", title,
                              {**payload, "snapshot": snapshot_identity(manifest)}, trace)
    plan = AnalysisPlan.model_validate(card["plan"])
    version = card.get("version")
    target = f" → {version['target_label']}" if version else ""
    session.reviews[card["record_id"]] = f"{plan.comparison_range.start} / {plan.time_range.start}{target} · {card['created_at'][11:19]}"
    agent.checkpoint(session)
    return card


def load_review(agent, session, card_id):
    manifest = agent.executors[session.dataset_id].manifest
    if card_id not in session.reviews:
        raise PlanError("请从当前会话的已保存复盘中选择")
    return read_evidence(manifest, session.session_id, card_id)


def build_review(agent, session, source_record):
    manifest = agent.executors[session.dataset_id].manifest
    source = read_evidence(manifest, session.session_id, source_record["record_id"])
    base = review_base(source)
    if base is None:
        raise PlanError("请先完成一次两期总体比较，再生成经营复盘")
    viewer = AnalysisAgent(None, agent.executors)
    isolated = Session(session.dataset_id, session_id=session.session_id)
    base = fixed_plan(base).model_copy(update={"metric_ids": METRICS, "output_mode": "analysis", "limitations": []})
    with TurnTrace(session.session_id, session.dataset_id, "计算经营复盘", "review") as trace:
        overview = source
        if set(source["plan"]["metric_ids"]) != set(METRICS):
            overview = viewer.run_plan(isolated, base, "经营复盘的两期指标", 0, trace,
                                       provenance={"source_record_id": source["record_id"]}).evidence
            if overview["result"]["status"] not in {"ok", "empty"}:
                raise PlanError(overview["result"]["error"])
        contribution = base.model_copy(update={"intent": "contribution", "dimension": "category", "metric_ids": ["sales"],
                                               "limit_mode": "all", "min_group_count": 1, "contribution_direction": "increase"})
        result = viewer.run_plan(isolated, contribution, "经营复盘的全部品类商品额贡献", 0, trace,
                                 provenance={"source_record_id": source["record_id"]}).evidence
    return _save(agent, session, {"source_record_id": source["record_id"], "plan": base.model_dump(mode="json"),
                                 "overview_id": overview["record_id"], "contribution_id": result["record_id"]})


def review_record(agent, session, card, key):
    manifest = agent.executors[session.dataset_id].manifest
    return read_evidence(manifest, session.session_id, card[key])


def _affected_orders(csv_file):
    groups, issues = {}, {}
    with Path(csv_file).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = json.loads(row["key"])
            if row["layer"] == "quality" and key[0] == "order_items" and key[2:] == ["price", "amount_conflict"]:
                issues.setdefault(key[1][0], []).append({"item_key": key[1], "change": row["change"]})
            if row["layer"] != "membership" or row["change"] not in {"eligibility_entered", "eligibility_exited", "scope_uncertain"}:
                continue
            period, metric, _, order_id, _ = key
            if metric not in {"sales", "freight"}:
                continue
            group = groups.setdefault((period, order_id, row["change"]), {})
            amount = json.loads(row["explanation"])["amount"]
            group[metric] = group.get(metric, Decimal(0)) + Decimal(amount["decimal"] if isinstance(amount, dict) else amount)
    return [{"period": period, "order_id": order_id, "change": change, "amounts": amounts, "issues": issues.get(order_id, [])}
            for (period, order_id, change), amounts in sorted(groups.items())]


def compare_review(agent, session, card_id, target):
    card = load_review(agent, session, card_id)
    if not target or target == session.dataset_id:
        raise PlanError("请选择另一套数据快照")
    overview = review_record(agent, session, card, "overview_id")
    viewer = AnalysisAgent(None, agent.executors)
    isolated = Session(session.dataset_id, session_id=session.session_id, analysis_evidence=overview)
    prior = session.comparison_state
    if prior.get("status") == "completed" and prior["original_record_id"] == overview["record_id"] and prior["target_dataset_id"] == target and prior["request"]["mode"] == "fixed_scope":
        state = prior
    else:
        state = viewer.compare(isolated, {"target_dataset_id": target, "source_record_id": overview["record_id"]}).evidence
    version = {key: state[key] for key in ("status", "artifact_id", "target_snapshot", "target_label", "contract_changes")}
    version["report_file"] = state["report_file"]
    version["bundle_file"] = state.get("bundle_file")
    if state["status"] == "completed":
        diff = state["stages"]["diff"]["report"]
        version["reconciliation"] = diff["reconciliation"]
        version["metric_comparisons"] = diff["metric_comparisons"]
        affected = _affected_orders(diff["csv_file"]) if not (state["contract_changes"] or diff["scope_changes"]) else []
        version["affected_count"] = len(affected)
        version["affected_orders"] = affected[:MAX_ROWS]
        version["scope_changes"] = diff["scope_changes"]
    else:
        version["errors"] = [stage["error"] for stage in state["stages"].values() if stage.get("error")]
    payload = {key: card[key] for key in ("source_record_id", "plan", "overview_id", "contribution_id")}
    return _save(agent, session, {**payload, "version": version})


def review_sources(agent, session, card_id, row_index):
    version = load_review(agent, session, card_id)["version"]
    row = version["affected_orders"][row_index]
    is_restored = row["change"] == "eligibility_entered"
    executor = agent.executors[session.dataset_id if is_restored else version["target_snapshot"]["dataset_id"]]
    if not is_restored and snapshot_identity(executor.manifest) != version["target_snapshot"]:
        raise PlanError("目标快照已改变，请在原快照上查验")
    source = retained_order_records(executor, row["order_id"])
    return {**source, "dataset": executor.manifest["name"], "comparison_evidence_id": version["artifact_id"], "affected_row": row}


def review_drill(agent, session, card_id, entity_id):
    card = load_review(agent, session, card_id)
    record = review_record(agent, session, card, "contribution_id")
    result = QueryResult.model_validate(record["result"])
    values = [None] * len(result.columns)
    values[result.columns.index("entity_id")] = entity_id
    index = selected_row_index(record, values)
    isolated = Session(session.dataset_id, session_id=session.session_id, last_evidence=record, analysis_evidence=record,
                       last_successful_plan=AnalysisPlan.model_validate(record["plan"]), last_result=result)
    return AnalysisAgent(None, agent.executors).drill(isolated, index, "seller").evidence
