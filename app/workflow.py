"""有序任务检查点；失败、澄清和依赖等待互不覆盖。"""
from app.models import AnalysisPlan, EntityFilter, Predicate
from app.semantic import PlanError
from app.grouping import KEY_COLUMNS
from app.quality import calculation_determined


def reconcile_tasks(previous, plan, tasks):
    retained = []
    changed = set()
    for index, step in enumerate(plan.steps):
        same = previous is not None and previous.intent == "workflow" and index < len(previous.steps) and previous.steps[index] == step
        invalid = not same or (step.source_step is not None and step.source_step-1 in changed)
        if invalid:
            changed.add(index)
        retained.append(tasks[index] if not invalid and index < len(tasks) else
                        {"status": "pending", "reason": "尚未执行", "evidence": {}, "text": ""})
    return retained


def bind_step(step, tasks):
    source = tasks[step.source_step-1]
    if source["status"] != "completed":
        return None, "pending", "等待依赖步骤完成"
    evidence = source["evidence"]
    result = evidence.get("result")
    if not result or result["status"] != "ok" or step.source_row >= len(result["rows"]):
        return None, "needs_input", "依赖结果为空或没有指定行，请明确目标实体"
    source_plan = evidence["plan"]
    dimension = source_plan["dimension"]
    expected = {"category": "category_id", "seller": "seller_id", "customer_state": "customer_state"}.get(dimension)
    if step.bind_mode == "field" and ((source_plan.get("group_by") or expected is None or step.bind_field != expected) and not (step.bind_field == "order_id" and "order_id" in result["columns"])):
        raise PlanError("依赖字段与来源结果粒度不一致")
    if step.bind_mode == "group" and not source_plan.get("group_by") and dimension == "total":
        raise PlanError("组合绑定的来源必须有分组")
    metric = source_plan["metric_ids"][0] if source_plan["metric_ids"] else None
    relevant = {metric, f"{metric}_current", f"{metric}_previous"}
    indexed = [(i, dict(zip(result["columns"], row))) for i, row in enumerate(result["rows"])]
    partition = source_plan.get("rank_within") or source_plan.get("contribution_within")
    if partition:
        field = KEY_COLUMNS[partition]
        parents = {row[field] for _, row in indexed}
        if step.source_partition is None and len(parents) > 1:
            return None, "needs_input", "来源含多个父分组，请明确 source_partition 后选择组内结果"
        chosen = step.source_partition if step.source_partition is not None else next(iter(parents))
        indexed = [(i, row) for i, row in indexed if row[field] == chosen]
    elif step.source_partition is not None:
        raise PlanError("来源没有排名分区")
    if step.source_row >= len(indexed):
        return None, "needs_input", "指定分区没有对应结果行，请明确目标组合"
    qualities = [result["quality"][i] for i, _ in indexed] if result.get("quality") else []
    if any(q and (not calculation_determined(q) or q.get("group_eligibility") == "uncertain") for row in qualities for name, q in row.items() if name in relevant):
        return None, "needs_input", "来源排名含未知候选，无法确定目标；请明确实体"
    rows = [row for _, row in indexed]
    if any("membership_confirmed" in row and row["membership_confirmed"] is not True for row in rows):
        return None, "needs_input", "来源明细含归属未知的候选，请明确目标实体"
    row = rows[step.source_row]
    key_column = "rank_no" if "rank_no" in row else (metric + "_change" if source_plan["intent"] == "contribution" else None)
    if key_column and (row[key_column] is None or sum(r[key_column] == row[key_column] for r in rows) > 1):
        return None, "needs_input", "来源结果并列或排名未知，请指定实体；不按行顺序选择第一名"
    if step.bind_mode == "group":
        from app.drilldown import group_scope
        scope = group_scope(AnalysisPlan.model_validate(source_plan), row)
        selected_fields = {f.field for f in scope["filters"]}
        scope["filters"] += [f for f in step.filters if f.field not in selected_fields]
        for name in ("order_where", "item_where"):
            if getattr(step, name):
                scope[name] = Predicate(children=[scope[name], getattr(step, name)]) if scope[name] else getattr(step, name)
        return step.model_copy(update={**scope, "source_step": None, "source_row": 0,
            "source_partition": None, "bind_field": None, "bind_mode": "field"}), "pending", ""
    key = row["order_id"] if step.bind_field == "order_id" else row["entity_id"]
    # 清除该字段在所有简单条件中的旧限制，再将依赖值绑定到本次执行副本。
    from app.plan_update import remove_field
    changes = {"source_step": None, "bind_field": None, "source_row": 0, "source_partition": None,
               "order_where": remove_field(step.order_where, step.bind_field),
               "item_where": remove_field(step.item_where, step.bind_field)}
    if step.bind_field == "order_id":
        changes["selected_order_id"] = key
        if source_plan.get("diagnosis") == "order_candidate":
            changes.update(audit_seller_id=row["seller_id"], filters=[], order_where=None, item_where=None)
    else:
        changes["filters"] = [f for f in step.filters if f.field != step.bind_field] + [EntityFilter(field=step.bind_field, values=[key])]
    return step.model_copy(update=changes), "pending", ""
