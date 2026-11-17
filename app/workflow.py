"""有序任务检查点；失败、澄清和依赖等待互不覆盖。"""
from app.models import EntityFilter
from app.semantic import PlanError


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
    dimension = evidence["plan"]["dimension"]
    expected = {"category": "category_id", "seller": "seller_id", "customer_state": "customer_state"}.get(dimension)
    if step.bind_field != expected and not (step.bind_field == "order_id" and "order_id" in result["columns"]):
        raise PlanError("依赖字段与来源结果粒度不一致")
    source_plan = evidence["plan"]
    metric = source_plan["metric_ids"][0] if source_plan["metric_ids"] else None
    relevant = {metric, f"{metric}_current", f"{metric}_previous"}
    if any(q and q["completeness"] != "complete" for row in result.get("quality", []) for name, q in row.items() if name in relevant):
        return None, "needs_input", "来源排名含未知候选，无法确定目标；请明确实体"
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    if any("membership_confirmed" in row and row["membership_confirmed"] is not True for row in rows):
        return None, "needs_input", "来源明细含归属未知的候选，请明确目标实体"
    row = rows[step.source_row]
    key_column = "rank_no" if "rank_no" in row else (source_plan["metric_ids"][0] + "_change" if source_plan["intent"] == "contribution" else None)
    if key_column and (row[key_column] is None or sum(r[key_column] == row[key_column] for r in rows) > 1):
        return None, "needs_input", "来源结果并列或排名未知，请指定实体；不按行顺序选择第一名"
    key = row["order_id"] if step.bind_field == "order_id" else row["entity_id"]
    # 清除该字段在所有简单条件中的旧限制，再将依赖值绑定到本次执行副本。
    from app.plan_update import remove_field
    changes = {"source_step": None, "bind_field": None, "source_row": 0,
               "order_where": remove_field(step.order_where, step.bind_field),
               "item_where": remove_field(step.item_where, step.bind_field)}
    if step.bind_field == "order_id":
        changes["selected_order_id"] = key
    else:
        changes["filters"] = [f for f in step.filters if f.field != step.bind_field] + [EntityFilter(field=step.bind_field, values=[key])]
    return step.model_copy(update=changes), "pending", ""
