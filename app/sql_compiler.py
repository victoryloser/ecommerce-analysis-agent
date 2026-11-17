"""由指标、维度、算子组合 SQL；用户值始终通过参数绑定。"""
from dataclasses import dataclass

from app.models import AnalysisPlan


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple


def compile_query(plan: AnalysisPlan) -> CompiledQuery:
    from app.advanced import extended, compile_advanced
    if extended(plan):
        return compile_advanced(plan)
    if plan.intent not in {"join_example", "compare"}:
        from app.facts import aggregate
        return aggregate(plan)
    if plan.intent == "join_example":
        return CompiledQuery("""WITH item_amounts AS (
            SELECT order_id, COUNT(*) AS item_rows, CASE WHEN COUNT(*)=COUNT(price) THEN SUM(price) END AS sales FROM order_items GROUP BY order_id
        ), payment_amounts AS (
            SELECT order_id, COUNT(*) AS payment_rows, CASE WHEN COUNT(*)=COUNT(payment_value) THEN SUM(payment_value) END AS payment_amount FROM order_payments GROUP BY order_id
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
            CAST(c.{metric}-p.{metric} AS DOUBLE)/NULLIF(p.{metric},0) AS change_rate,
            json_object('current_value',json_extract(c.__quality,'$.{metric}'),
                        'previous_value',json_extract(p.__quality,'$.{metric}')) AS __quality
            FROM ({current.sql}) c CROSS JOIN ({previous.sql}) p"""
        return CompiledQuery(sql, current.parameters + previous.parameters)
