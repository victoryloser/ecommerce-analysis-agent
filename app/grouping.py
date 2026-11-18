"""计划、事实查询和版本比较共用的分组身份与列投影。"""

KEY_COLUMNS = {"month": "month", "category": "category_id", "seller": "seller_id",
               "customer_state": "customer_state", "delivery_status": "delivery_status"}
NAME_COLUMNS = {"category": "category_name", "seller": "seller_name"}
DIMENSION_LABELS = {"category": "品类", "seller": "商家", "month": "月份", "customer_state": "客户省份"}
REGIONAL_GROUP = ("category", "customer_state")
REGIONAL_METRICS = {"sales", "freight", "order_count"}
JOINT_GROUPS = (("category", "seller"), ("month", "category"), REGIONAL_GROUP)


def dimensions(plan):
    return tuple(plan.group_by) if plan.group_by else (() if plan.dimension == "total" else (plan.dimension,))


def result_columns(plan):
    return [column for dim in dimensions(plan) for column in
            ([KEY_COLUMNS[dim], NAME_COLUMNS[dim]] if dim in NAME_COLUMNS else [KEY_COLUMNS[dim]])] if plan.group_by else []


def dimension_sql(plan, dim, item="i", order="o"):
    if dim == "category":
        return f"{item}.category_id", f"{item}.category_name_zh"
    if dim == "seller":
        return f"{item}.seller_id", f"{item}.seller_name"
    if dim == "month":
        expr = f"strftime({order}.{plan.time_field},'%Y-%m')"
        return expr, expr
    if dim == "customer_state":
        return f"{order}.customer_state", f"{order}.customer_state"
    condition = f"{order}.delivered_at IS NULL OR {order}.estimated_delivery_at IS NULL"
    late = f"{order}.delivered_at>{order}.estimated_delivery_at"
    return (f"CASE WHEN {condition} THEN 'unknown' WHEN {late} THEN 'delayed' ELSE 'on_time' END",
            f"CASE WHEN {condition} THEN '时间缺失' WHEN {late} THEN '延迟' ELSE '准时' END")


def entity_sql(plan, item="i", order="o"):
    dims = dimensions(plan)
    if not dims:
        return "'all'", "'全站'"
    pairs = [dimension_sql(plan, d, item, order) for d in dims]
    if len(dims) == 1:
        return pairs[0]
    key = "CAST(json_object(" + ",".join(f"'{d}',{pair[0]}" for d, pair in zip(dims, pairs)) + ") AS VARCHAR)"
    name = "concat_ws(' / '," + ",".join(f"COALESCE({pair[1]},'未标注')" for pair in pairs) + ")"
    return key, name


def joint_projection(plan, item="i", order="o"):
    fields = []
    if plan.group_by:
        for dim in dimensions(plan):
            key, name = dimension_sql(plan, dim, item, order)
            fields.append(f"{key} AS {KEY_COLUMNS[dim]}")
            if dim in NAME_COLUMNS:
                fields.append(f"{name} AS {NAME_COLUMNS[dim]}")
    return fields
