"""使用结果行的稳定ID下钻，不解析展示文本，也不再调用模型。"""
from datetime import date

from app.models import AnalysisPlan, EntityFilter, QueryResult, TimeRange, Predicate
from app.semantic import PlanError, validate_plan
from app.grouping import dimensions, KEY_COLUMNS

TARGETS = {"auto": "自动细分", "total": "仅当前分组", "month": "月份", "category": "品类", "seller": "商家", "customer_state": "客户省份",
           "orders": "订单明细", "items": "商品明细", "payments": "支付记录", "reviews": "评价详情", "audit_order": "核查整单（商家小计及全部明细）"}


def selected_row_index(evidence: dict, displayed_row: list) -> int:
    from app.presentation import display_evidence
    evidence = display_evidence(evidence)
    if evidence.get("type") != "query":
        raise PlanError("请选择带分组ID的结构化分析结果行")
    result = evidence["result"]
    if "entity_id" not in result["columns"] and len(result["rows"]) == 1:
        return 0
    column = result["columns"].index("entity_id")
    entity_id = displayed_row[column]
    # 表格排序后视觉行号会改变，因此按结果列中的稳定ID定位。
    indices = [index for index, row in enumerate(result["rows"]) if str(row[column]) == str(entity_id)]
    if len(indices) != 1:
        raise PlanError("所选分组不在当前结果中，请重新选择")
    return indices[0]


def group_scope(parent: AnalysisPlan, selected: dict) -> dict:
    filters = [f.model_copy(deep=True) for f in parent.filters]
    period = parent.time_range
    order_where, item_where = parent.order_where, parent.item_where
    for dim in dimensions(parent):
        value = selected[KEY_COLUMNS[dim]] if parent.group_by else selected["entity_id"]
        if dim == "month":
            year, month = map(int, value.split("-"))
            start, end = date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)
            period = TimeRange(start=max(period.start, start), end=min(period.end, end))
        else:
            field = KEY_COLUMNS[dim]
            filters = [f for f in filters if f.field != field]
            # 保留原布尔条件，将选中分组作为额外 AND 约束。
            if value is not None and dim in {"category", "seller", "customer_state"}:
                filters.append(EntityFilter(field=field, values=[value]))
            else:
                condition = Predicate(field=field, operator="is_null" if value is None else "eq", values=[] if value is None else [value])
                if dim in {"category", "seller"}:
                    item_where = Predicate(children=[item_where, condition]) if item_where else condition
                else:
                    order_where = Predicate(children=[order_where, condition]) if order_where else condition
    return {"filters": filters, "time_range": period, "time_field": parent.time_field,
            "order_statuses": list(parent.order_statuses), "order_where": order_where, "item_where": item_where,
            "period_mode": "explicit_range", "selected_order_id": parent.selected_order_id}


def drill_plan(parent: AnalysisPlan, result: QueryResult, row_index: int, target: str, manifest: dict) -> AnalysisPlan:
    if parent.intent not in {"aggregate", "rank", "compare", "contribution", "details", "diagnose"}:
        raise PlanError("当前分析不支持下钻")
    if target == "auto":
        target = ("seller" if "month" in dimensions(parent) else "orders") if parent.group_by else "items" if parent.intent == "details" else {"total": "category", "month": "category", "category": "seller", "seller": "orders", "customer_state": "category", "delivery_status": "orders"}[parent.dimension]
        if set(parent.metric_ids) & {"payment_amount", "repeat_customers"}:
            target = "total" if parent.dimension == "month" else "month"
    if target not in TARGETS:
        raise PlanError("请选择下钻维度")
    if not 0 <= row_index < len(result.rows):
        raise PlanError("所选行不在当前查询结果中")
    selected = dict(zip(result.columns, result.rows[row_index]))
    entity_id = selected.get("entity_id", "all")
    filters = [f.model_copy(deep=True) for f in parent.filters]
    period = parent.time_range
    order_where = parent.order_where
    selected_order_id = parent.selected_order_id
    if parent.intent == "details" or (parent.intent == "diagnose" and "order_id" in selected):
        selected_order_id = selected["order_id"]
    elif parent.dimension == "month":
        year, month = map(int, entity_id.split("-"))
        start = date(year, month, 1)
        end = date(year + (month == 12), month % 12 + 1, 1)
        period = TimeRange(start=max(period.start, start), end=min(period.end, end))
    elif parent.dimension in {"category", "seller", "customer_state"}:
        field = {"category": "category_id", "seller": "seller_id", "customer_state": "customer_state"}[parent.dimension]
        filters = [f for f in filters if f.field != field]
        if entity_id is None:
            condition = Predicate(field=field, operator="is_null")
            order_where = Predicate(children=[order_where, condition]) if order_where else condition
        else:
            filters.append(EntityFilter(field=field, values=[entity_id]))
    elif parent.dimension == "delivery_status":
        condition = Predicate(field="delivery_status", values=[entity_id])
        order_where = Predicate(children=[order_where, condition]) if order_where else condition
    plan = parent.model_copy(update={"intent": "aggregate", "dimension": target, "time_range": period,
        "group_by": None, "rank_within": None, "contribution_within": None,
        "period_mode": "explicit_range", "comparison_mode": "none",
        "filters": filters, "comparison_range": None, "min_group_count": 1, "top_n": 10,
        "order_where": order_where, "selected_order_id": selected_order_id})
    if parent.group_by:
        plan = plan.model_copy(update=group_scope(parent, selected))
    if target in {"orders", "items", "payments", "reviews"}:
        plan = plan.model_copy(update={"intent": "details", "dimension": "total", "metric_ids": [], "detail_level": target})
    validate_plan(plan, manifest)
    return plan
