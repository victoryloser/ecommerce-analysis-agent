"""由指标、维度、算子组合 SQL；用户值始终通过参数绑定。"""
from dataclasses import dataclass

from app.models import AnalysisPlan
from app.metadata import EXPRESSIONS


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple


def compile_query(plan: AnalysisPlan) -> CompiledQuery:
    from app.advanced import extended, compile_advanced
    if extended(plan):
        return compile_advanced(plan)
    if plan.intent == "join_example":
        return CompiledQuery("""WITH item_amounts AS (
            SELECT order_id, COUNT(*) AS item_rows, SUM(price) AS sales FROM order_items GROUP BY order_id
        ), payment_amounts AS (
            SELECT order_id, COUNT(*) AS payment_rows, SUM(payment_value) AS payment_amount FROM order_payments GROUP BY order_id
        ) SELECT o.order_id, i.item_rows, p.payment_rows, i.item_rows*p.payment_rows AS joined_rows,
            i.sales, p.payment_amount, i.sales*p.payment_rows AS inflated_sales,
            p.payment_amount*i.item_rows AS inflated_payment
        FROM orders o JOIN item_amounts i ON i.order_id=o.order_id
        JOIN payment_amounts p ON p.order_id=o.order_id
        WHERE o.order_status='delivered' AND o.purchase_at>=? AND o.purchase_at<?
            AND i.item_rows>1 AND p.payment_rows>1
        ORDER BY o.order_id LIMIT 1""", (plan.time_range.start, plan.time_range.end))
    if plan.intent == "compare":
        current = compile_query(plan.model_copy(update={"intent": "aggregate", "comparison_range": None}))
        previous = compile_query(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None}))
        metric = plan.metric_ids[0]
        sql = f"""SELECT c.{metric} AS current_value, p.{metric} AS previous_value,
            c.{metric}-p.{metric} AS absolute_change,
            CAST(c.{metric}-p.{metric} AS DOUBLE)/NULLIF(p.{metric},0) AS change_rate
            FROM ({current.sql}) c CROSS JOIN ({previous.sql}) p"""
        return CompiledQuery(sql, current.parameters + previous.parameters)

    parameters = [plan.time_range.start, plan.time_range.end]
    order_filters = ["o.order_status='delivered'", "o.purchase_at>=?", "o.purchase_at<?"]
    item_filters = []
    # 参数顺序和 CTE 中占位符出现顺序一致。
    for filter_ in plan.filters:
        if filter_.field == "customer_state":
            order_filters.append(f"c.customer_state IN ({','.join('?' for _ in filter_.values)})")
            parameters.extend(filter_.values)
    for filter_ in plan.filters:
        if filter_.field != "customer_state":
            field = {"category_id": "p.category_id", "seller_id": "i.seller_id"}[filter_.field]
            item_filters.append(f"{field} IN ({','.join('?' for _ in filter_.values)})")
            parameters.extend(filter_.values)
    ctes = [f"""eligible_orders AS (
        SELECT o.*, c.customer_unique_id, c.customer_state FROM orders o
        JOIN customers c ON c.customer_id=o.customer_id WHERE {' AND '.join(order_filters)}
    )""", f"""items AS (
        SELECT i.*, p.category_id, cat.category_name_zh, s.seller_name
        FROM order_items i JOIN eligible_orders o ON o.order_id=i.order_id
        JOIN products p ON p.product_id=i.product_id
        JOIN categories cat ON cat.category_id=p.category_id
        JOIN sellers s ON s.seller_id=i.seller_id
        {'WHERE ' + ' AND '.join(item_filters) if item_filters else ''}
    )""", f"""scoped_orders AS (
        SELECT * FROM eligible_orders o
        {'WHERE EXISTS (SELECT 1 FROM items i WHERE i.order_id=o.order_id)' if item_filters else ''}
    )"""]

    dimension = plan.dimension
    if dimension in {"seller", "category"}:
        entity_id, entity_name = {"seller": ("seller_id", "seller_name"), "category": ("category_id", "category_name_zh")}[dimension]
        ctes.append(f"entity_items AS (SELECT order_id, {entity_id} AS entity_id, {entity_name} AS entity_name, SUM(price) AS sales FROM items GROUP BY order_id, {entity_id}, {entity_name})")
        base_from = "scoped_orders o JOIN entity_items i ON i.order_id=o.order_id"
        entity_expr = "i.entity_id, i.entity_name"
    else:
        ctes.append("entity_items AS (SELECT order_id, SUM(price) AS sales FROM items GROUP BY order_id)")
        base_from = "scoped_orders o LEFT JOIN entity_items i ON i.order_id=o.order_id"
        entity_expr = {
            "total": "'all' AS entity_id, '全站' AS entity_name",
            "month": "strftime(o.purchase_at, '%Y-%m') AS entity_id, strftime(o.purchase_at, '%Y-%m') AS entity_name",
            "customer_state": "o.customer_state AS entity_id, o.customer_state AS entity_name",
            "delivery_status": "CASE WHEN o.delivered_at IS NULL OR o.estimated_delivery_at IS NULL THEN 'unknown' WHEN o.delivered_at>o.estimated_delivery_at THEN 'delayed' ELSE 'on_time' END AS entity_id, CASE WHEN o.delivered_at IS NULL OR o.estimated_delivery_at IS NULL THEN '时间缺失' WHEN o.delivered_at>o.estimated_delivery_at THEN '延迟' ELSE '准时' END AS entity_name",
        }[dimension]
    extras = []
    if "payment_amount" in plan.metric_ids:
        ctes.append("payments AS (SELECT order_id, SUM(payment_value) AS payment_amount FROM order_payments GROUP BY order_id)")
        base_from += " LEFT JOIN payments pay ON pay.order_id=o.order_id"
        extras.append("pay.payment_amount")
    if set(plan.metric_ids) & {"avg_review", "low_score_rate"}:
        base_from += " LEFT JOIN order_reviews r ON r.order_id=o.order_id"
        extras.append("r.review_score")
    ctes.append(f"facts AS (SELECT o.*, i.sales, {entity_expr}{',' + ','.join(extras) if extras else ''} FROM {base_from})")
    # 总量查询不加 GROUP BY，即便无订单也返回真实的 0 / 不可计算的比率。
    group = "" if dimension == "total" else "GROUP BY entity_id, entity_name"
    entity_select = "" if dimension == "total" else "entity_id, entity_name, "
    if plan.metric_ids == ["repeat_customers"]:
        group_columns = "" if dimension == "total" else "entity_id, entity_name, "
        ctes.append(f"customer_orders AS (SELECT {group_columns}customer_unique_id, COUNT(DISTINCT order_id) AS n FROM facts GROUP BY {group_columns}customer_unique_id)")
        body = f"SELECT {entity_select}COUNT(*) FILTER (WHERE n>=2) AS repeat_customers FROM customer_orders {group}"
    else:
        expressions = [f"{EXPRESSIONS[m]} AS {m}" for m in plan.metric_ids]
        if "delay_rate" in plan.metric_ids:
            expressions += ["COUNT(*) AS sample_orders", "COUNT(*) FILTER (WHERE delivered_at>estimated_delivery_at) AS delayed_orders", "COUNT(*) FILTER (WHERE delivered_at IS NULL OR estimated_delivery_at IS NULL) AS missing_timing_orders"]
        if set(plan.metric_ids) & {"avg_review", "low_score_rate"}:
            expressions += ["COUNT(review_score) AS reviewed_orders"]
        having = f"HAVING COUNT(DISTINCT order_id)>={plan.min_group_count}" if dimension != "total" else ""
        body = f"SELECT {entity_select}{', '.join(expressions)} FROM facts {group} {having}"
    ctes.append(f"aggregated AS ({body})")
    if plan.intent == "rank":
        metric = plan.metric_ids[0]
        ctes.append(f"ranked AS (SELECT *, RANK() OVER (ORDER BY {metric} DESC NULLS LAST) AS rank_no FROM aggregated)")
        body = f"SELECT * FROM ranked WHERE rank_no<={plan.top_n} ORDER BY {metric} DESC NULLS LAST, entity_id"
    else:
        body = "SELECT * FROM aggregated" + (" ORDER BY entity_id" if dimension != "total" else "")
    return CompiledQuery("WITH " + ",\n".join(ctes) + "\n" + body, tuple(parameters))
