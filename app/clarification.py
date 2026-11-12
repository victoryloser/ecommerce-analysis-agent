"""只对明确的槽位回答做确定性补全，新的自由问题仍交给模型。"""
import re
from datetime import date
from typing import get_args

from app.models import AnalysisIntent, AnalysisPlan, EntityCandidate, EntityFilter, TimeRange
from app.semantic import PlanError


def month_range(year: int, month: int) -> TimeRange:
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return TimeRange(start=date(year, month, 1), end=end)


def entity_values(manifest: dict) -> dict[str, list[tuple[str, str]]]:
    entities = manifest["entities"]
    return {"category_id": [tuple(r) for r in entities["categories"]],
            "seller_id": [tuple(r) for r in entities["sellers"]],
            "customer_state": [(v, v) for v in entities["customer_states"]]}


def map_entities(plan: AnalysisPlan, replace) -> AnalysisPlan:
    """普通实体筛选和布尔条件的实体等值/集合叶子共用绑定与选择逻辑。"""
    def predicate(node):
        if node is None:
            return None
        if node.children:
            return node.model_copy(update={"children": [predicate(child) for child in node.children]})
        if node.field in {"category_id", "seller_id", "customer_state"} and node.operator in {"eq", "ne", "in", "not_in"}:
            return node.model_copy(update={"values": [replace(node.field, value, False) if isinstance(value, str) else value for value in node.values]})
        return node

    return plan.model_copy(update={
        "filters": [EntityFilter(field=f.field, values=[replace(f.field, value, True) for value in f.values]) for f in plan.filters],
        "order_where": predicate(plan.order_where), "item_where": predicate(plan.item_where)})


def slot_prompt(plan: AnalysisPlan) -> str:
    if "year" in plan.missing_slots:
        return f"请补充{plan.month}月的年份。"
    if plan.entity_candidates:
        return f"“{plan.ambiguous_value}”对应多个实体，请选择具体ID：" + "、".join(c.label for c in plan.entity_candidates)
    labels = {"metric_ids": "指标", "time_range": "统计期间", "category_id": "品类ID", "seller_id": "商家ID", "customer_state": "客户省份"}
    return "请补充：" + "、".join(labels[slot] for slot in plan.missing_slots)


def bind_entities(plan: AnalysisPlan, manifest: dict, question: str = "") -> AnalysisPlan:
    if plan.intent == "workflow" or (plan.intent == "clarify" and plan.pending_intent == "workflow"):
        steps = [bind_entities(step, manifest, question) for step in plan.steps]
        active = next(((index, step) for index, step in enumerate(steps, 1) if step.intent == "clarify"), None)
        return plan.model_copy(update={"steps": steps, "intent": "clarify" if active else "workflow",
            "pending_intent": "workflow" if active else None,
            "clarification": f"步骤{active[0]}：{active[1].clarification}" if active else None,
            "missing_slots": [], "entity_candidates": [], "ambiguous_value": None})
    intent = plan.pending_intent if plan.intent == "clarify" else plan.intent
    if intent not in get_args(AnalysisIntent):
        return plan
    dictionaries = entity_values(manifest)
    names = {}
    for field, records in dictionaries.items():
        names[field] = {}
        for key, label in records:
            names[field].setdefault(label.casefold(), []).append((key, label))
    ids = {field: dict(records) for field, records in dictionaries.items()}
    ambiguities = []

    def bind(field, value, require_known):
        # 模型替同名实体猜了ID时，恢复为名称，让用户选择。
        if value in ids[field]:
            label = ids[field][value]
            same_name = names[field][label.casefold()]
            if len(same_name) > 1 and label.casefold() in question.casefold() and not any(key in question for key, _ in same_name):
                value = label
        if value in ids[field]:
            return value
        matches = names[field].get(value.casefold(), [])
        if not matches:
            if require_known:
                raise PlanError(f"{field} 无匹配实体：{value}")
            return value  # SQL条件允许显式的不存在值，如NOT IN。
        if len(matches) == 1:
            return matches[0][0]
        ambiguities.append((field, matches[0][1], matches))
        return matches[0][1]

    bound = map_entities(plan, bind)
    if not ambiguities:
        return bound
    field, value, matches = ambiguities[0]
    slots = list(dict.fromkeys([*plan.missing_slots, field]))
    bound = bound.model_copy(update={"intent": "clarify", "pending_intent": intent,
        "missing_slots": slots, "ambiguous_value": value,
        "entity_candidates": [EntityCandidate(field=field, value=key, label=f"{label}（{key}）") for key, label in matches]})
    return bound.model_copy(update={"clarification": slot_prompt(bound)})


def resolve_slots(pending: AnalysisPlan | None, question: str) -> AnalysisPlan | None:
    if pending is None or pending.pending_intent is None:
        return None
    if pending.pending_intent == "workflow":
        index = next(i for i, step in enumerate(pending.steps) if step.intent == "clarify")
        completed = resolve_slots(pending.steps[index], question)
        if completed is None:
            return None
        steps = pending.steps.copy()
        steps[index] = completed
        return pending.model_copy(update={"intent": "workflow", "pending_intent": None, "clarification": None, "steps": steps})

    updates = {}
    year = re.fullmatch(r"\s*(\d{4})\s*年?\s*", question)
    if "year" in pending.missing_slots and pending.month and year:
        value = int(year.group(1))
        updates.update(time_range=month_range(value, pending.month), month=None, comparison_month=None)
        if pending.comparison_month:
            comparison_year = value - 1 if pending.comparison_month > pending.month else value
            updates["comparison_range"] = month_range(comparison_year, pending.comparison_month)
        slot = "year"
    else:
        selected = [c for c in pending.entity_candidates if question.strip() in {c.value, c.label}]
        if len(selected) != 1:
            return None
        candidate = selected[0]
        updated = map_entities(pending, lambda field, value, _: candidate.value if field == candidate.field and value == pending.ambiguous_value else value)
        updates.update(filters=updated.filters, order_where=updated.order_where, item_where=updated.item_where,
                       entity_candidates=[], ambiguous_value=None)
        slot = candidate.field
    remaining = [name for name in pending.missing_slots if name != slot]
    updates.update(missing_slots=remaining, intent="clarify" if remaining else pending.pending_intent,
                   pending_intent=pending.pending_intent if remaining else None, clarification=None)
    completed = pending.model_copy(update=updates)
    if remaining:
        completed = completed.model_copy(update={"clarification": slot_prompt(completed)})
    return completed


def choices(plan: AnalysisPlan, manifest: dict) -> list[tuple[str, str]]:
    if plan.pending_intent == "workflow":
        return choices(next(step for step in plan.steps if step.intent == "clarify"), manifest)
    if "year" in plan.missing_slots:
        coverage = manifest.get("coverage_fields", {"purchase_at": manifest["coverage"]})[plan.time_field]
        start, end = coverage["start"], coverage["end"]
        last_year = int(end[:4]) - (end[5:] == "01-01")
        return [(f"{year}年", f"{year}年") for year in range(int(start[:4]), last_year + 1)]
    return [(candidate.label, candidate.value) for candidate in plan.entity_candidates]
