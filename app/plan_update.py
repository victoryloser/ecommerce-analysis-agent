"""聊天与界面共用条件修改，工作流保留未绑定的依赖模板。"""
from datetime import timedelta

from app.models import AnalysisPlan, PlanUpdate, Predicate, TimeRange
from app.semantic import PlanError


def contains(node, field):
    return bool(node and (node.field == field or any(contains(child, field) for child in node.children)))


def remove_field(node, field):
    if not contains(node, field):
        return node
    if node.negate or (node.children and node.conjunction == "any"):
        raise PlanError(f"{field} 位于 OR/NOT 条件中，请明确要替换的整个条件组")
    if not node.children:
        return None
    children = [value for child in node.children if (value := remove_field(child, field)) is not None]
    return node.model_copy(update={"children": children}) if children else None


def expand_periods(plan, manifest):
    changes = {}
    if plan.period_mode == "full_coverage":
        coverage = manifest.get("coverage_fields", {}).get(plan.time_field, manifest["coverage"])
        changes["time_range"] = TimeRange(start=coverage["start"], end=coverage["end"])
    current = changes.get("time_range", plan.time_range)
    if plan.comparison_mode == "previous_month" and current:
        first = current.start.replace(day=1)
        changes["comparison_range"] = TimeRange(start=(first-timedelta(days=1)).replace(day=1), end=first)
    if plan.steps:
        changes["steps"] = [expand_periods(step, manifest) for step in plan.steps]
    return plan.model_copy(update=changes)


def apply_update(plan: AnalysisPlan, update: PlanUpdate, manifest: dict) -> AnalysisPlan:
    if plan.intent == "workflow":
        if update.target_step is None or update.target_step > len(plan.steps):
            raise PlanError("请指定要修改的工作流步骤")
        steps = list(plan.steps)
        steps[update.target_step-1] = apply_update(steps[update.target_step-1], update, manifest)
        return plan.model_copy(update={"steps": steps})
    field = update.field
    changes = {}
    if update.operation == "reset":
        changes = {"filters": [], "order_where": None, "item_where": None, "selected_order_id": None,
                   "source_step": None, "bind_field": None, "source_row": 0}
    elif field in {"order_where", "item_where"}:
        changes[field] = update.predicate if update.operation == "set" else None
        if update.predicate:
            changes["filters"] = [f for f in plan.filters if not contains(update.predicate, f.field)]
            if plan.bind_field and contains(update.predicate, plan.bind_field):
                changes.update(source_step=None, bind_field=None, source_row=0)
    elif field in {"time_range", "comparison_range"}:
        changes[field] = update.period
        if field == "time_range":
            changes["period_mode"] = update.period_mode or "explicit_range"
            if update.period is None and changes["period_mode"] != "full_coverage":
                raise PlanError("统计期间不能删除；可改为全期")
        else:
            mode = update.comparison_mode or ("none" if update.operation == "remove" else "fixed")
            changes["comparison_mode"] = mode
            if mode == "none":
                changes.update(intent="aggregate", comparison_range=None)
            elif plan.intent not in {"compare", "contribution"}:
                changes["intent"] = "compare"
    elif field in {"dimension", "metric_ids", "order_statuses", "time_field"}:
        if update.operation != "set" or not update.values:
            raise PlanError(f"{field} 需要提供替换值")
        changes[field] = update.values if field in {"metric_ids", "order_statuses"} else update.values[0]
    elif field:
        changes = {"filters": [f for f in plan.filters if f.field != field],
                   "order_where": remove_field(plan.order_where, field),
                   "item_where": remove_field(plan.item_where, field)}
        if field == "order_id":
            changes["selected_order_id"] = None
        if plan.bind_field == field:
            changes.update(source_step=None, bind_field=None, source_row=0)
        if update.operation == "set":
            node = update.predicate or Predicate(field=field, operator="in", values=update.values)
            if node.field != field:
                raise PlanError("替换字段必须与新条件一致")
            target = "item_where" if field in {"category_id", "seller_id", "product_id", "price", "freight_value"} else "order_where"
            old = changes[target]
            changes[target] = Predicate(children=[old, node]) if old else node
    else:
        raise PlanError("条件更新缺少字段")
    result = AnalysisPlan.model_validate(plan.model_copy(update=changes).model_dump())
    return expand_periods(result, manifest)


def replay_intent(plan, manifest):
    changes = {"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"]}
    if plan.steps:
        changes["steps"] = [replay_intent(step, manifest) for step in plan.steps]
    return expand_periods(plan.model_copy(update=changes), manifest)
