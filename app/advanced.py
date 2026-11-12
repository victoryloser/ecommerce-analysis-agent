"""有明确粒度和口径的扩展编译：条件、分组比较、贡献、明细及诊断。"""
import sqlglot
from sqlglot import exp

from app.models import AnalysisPlan
from app.predicates import ITEM_FIELDS, ORDER_FIELDS, predicate_sql
from app.sql_compiler import CompiledQuery


def extended(plan: AnalysisPlan) -> bool:
    return (plan.intent in {"contribution", "details", "diagnose"} or plan.time_field != "purchase_at"
            or plan.order_statuses != ["delivered"] or plan.order_where is not None or plan.item_where is not None
            or plan.selected_order_id is not None or (plan.intent == "compare" and (plan.dimension != "total" or len(plan.metric_ids) != 1)))


def aggregate(plan: AnalysisPlan) -> CompiledQuery:
    from app.sql_compiler import compile_query
    simple = plan.model_copy(update={"intent": "rank" if plan.intent == "rank" else "aggregate", "comparison_range": None,
        "time_field": "purchase_at", "order_statuses": ["delivered"], "order_where": None, "item_where": None,
        "selected_order_id": None})
    base = compile_query(simple)
    tree = sqlglot.parse_one(base.sql, read="duckdb")
    ctes = {c.alias: c.this for c in tree.args["with_"].expressions}
    conditions = [f"o.order_status IN ({','.join('?' for _ in plan.order_statuses)})", f"o.{plan.time_field}>=?", f"o.{plan.time_field}<?"]
    parameters = [*plan.order_statuses, plan.time_range.start, plan.time_range.end]
    for filter_ in plan.filters:
        if filter_.field == "customer_state":
            conditions.append(f"c.customer_state IN ({','.join('?' for _ in filter_.values)})")
            parameters.extend(filter_.values)
    order_sql, order_parameters = predicate_sql(plan.order_where, ORDER_FIELDS)
    if order_sql:
        conditions.append(f"({order_sql})")
        parameters.extend(order_parameters)
    if plan.selected_order_id:
        conditions.append("o.order_id=?")
        parameters.append(plan.selected_order_id)
    ctes["eligible_orders"].set("where", exp.Where(this=sqlglot.parse_one(" AND ".join(conditions), read="duckdb")))
    item_conditions = []
    for filter_ in plan.filters:
        if filter_.field != "customer_state":
            field = ITEM_FIELDS[filter_.field]
            item_conditions.append(f"{field} IN ({','.join('?' for _ in filter_.values)})")
            parameters.extend(filter_.values)
    item_sql, item_parameters = predicate_sql(plan.item_where, ITEM_FIELDS)
    if item_sql:
        item_conditions.append(f"({item_sql})")
        parameters.extend(item_parameters)
    ctes["items"].set("where", exp.Where(this=sqlglot.parse_one(" AND ".join(item_conditions), read="duckdb")) if item_conditions else None)
    ctes["scoped_orders"].set("where", exp.Where(this=sqlglot.parse_one("EXISTS (SELECT 1 FROM items i WHERE i.order_id=o.order_id)", read="duckdb")) if item_conditions else None)
    # 月份分组与当前选择的日期字段一致，仅替换事实层引用，不改查询期间。
    if plan.time_field != "purchase_at":
        for column in ctes["facts"].find_all(exp.Column):
            if column.table == "o" and column.name == "purchase_at":
                column.set("this", exp.to_identifier(plan.time_field))
    return CompiledQuery(tree.sql(dialect="duckdb"), tuple(parameters))


def comparison(plan: AnalysisPlan) -> CompiledQuery:
    metrics = list(plan.metric_ids)
    if plan.min_group_count > 1 and "order_count" not in metrics:
        metrics.append("order_count")
    current = aggregate(plan.model_copy(update={"intent": "aggregate", "comparison_range": None, "min_group_count": 1, "metric_ids": metrics}))
    previous = aggregate(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None, "min_group_count": 1, "metric_ids": metrics}))
    grouping = plan.dimension != "total"
    projections = ["COALESCE(c.entity_id,p.entity_id) AS entity_id", "COALESCE(c.entity_name,p.entity_name) AS entity_name"] if grouping else []
    additive = {"sales", "order_count", "payment_amount", "repeat_customers"}
    for metric in plan.metric_ids:
        c = f"COALESCE(c.{metric},0)" if metric in additive else f"c.{metric}"
        p = f"COALESCE(p.{metric},0)" if metric in additive else f"p.{metric}"
        projections += [f"{c} AS {metric}_current", f"{p} AS {metric}_previous",
                        f"{c}-{p} AS {metric}_change", f"CAST({c}-{p} AS DOUBLE)/NULLIF({p},0) AS {metric}_rate"]
    if plan.min_group_count > 1:
        projections += ["COALESCE(c.order_count,0) AS current_orders", "COALESCE(p.order_count,0) AS previous_orders"]
    join = "FULL OUTER JOIN previous p ON c.entity_id IS NOT DISTINCT FROM p.entity_id" if grouping else "CROSS JOIN previous p"
    sql = f"WITH current AS ({current.sql}), previous AS ({previous.sql}), changes AS (SELECT {','.join(projections)} FROM current c {join}) "
    if plan.intent == "contribution":
        metric = plan.metric_ids[0]
        sql += f", contributions AS (SELECT *, SUM({metric}_change) OVER () AS total_change, CAST({metric}_change AS DOUBLE)/NULLIF(SUM({metric}_change) OVER (),0) AS contribution_rate FROM changes) SELECT * FROM contributions"
        if plan.min_group_count > 1:
            sql += f" WHERE current_orders>={plan.min_group_count} AND previous_orders>={plan.min_group_count}"
        sql += f" ORDER BY {metric}_change DESC, entity_id"
    else:
        sql += "SELECT * FROM changes"
        if plan.min_group_count > 1:
            sql += f" WHERE current_orders>={plan.min_group_count} AND previous_orders>={plan.min_group_count}"
        sql += " ORDER BY entity_id" if grouping else ""
    return CompiledQuery(sql, current.parameters + previous.parameters)


def details(plan: AnalysisPlan) -> CompiledQuery:
    base = aggregate(plan.model_copy(update={"intent": "aggregate", "metric_ids": ["sales"], "dimension": "total", "min_group_count": 1}))
    tree = sqlglot.parse_one(base.sql, read="duckdb")
    prefix = exp.With(expressions=tree.args["with_"].expressions[:3]).sql(dialect="duckdb")
    if plan.detail_level == "orders":
        sql = """SELECT o.order_id AS entity_id,o.order_id AS entity_name,o.order_id,o.order_status,o.purchase_at,o.delivered_at,
            o.approved_at,o.shipped_at,o.estimated_delivery_at,c.customer_unique_id,c.customer_state,
            (SELECT SUM(x.price) FROM order_items x WHERE x.order_id=o.order_id) AS order_sales,
            (SELECT SUM(x.freight_value) FROM order_items x WHERE x.order_id=o.order_id) AS order_freight,
            (SELECT SUM(x.payment_value) FROM order_payments x WHERE x.order_id=o.order_id) AS payment_amount,
            (SELECT CASE WHEN COUNT(*)=1 THEN MAX(x.review_score) ELSE NULL END FROM order_reviews x WHERE x.order_id=o.order_id) AS review_score
            FROM scoped_orders o JOIN customers c ON c.customer_id=o.customer_id ORDER BY o.order_id"""
    elif plan.detail_level == "items":
        sql = """SELECT i.order_id||':'||CAST(i.order_item_id AS VARCHAR) AS entity_id,i.product_id AS entity_name,
            i.order_id,i.order_item_id,i.product_id,p.product_name,i.category_id,i.category_name_zh,i.seller_id,i.seller_name,i.price,i.freight_value
            FROM items i JOIN scoped_orders o ON o.order_id=i.order_id JOIN products p ON p.product_id=i.product_id ORDER BY i.order_id,i.order_item_id"""
    elif plan.detail_level == "payments":
        sql = """SELECT p.order_id||':'||CAST(p.payment_sequence AS VARCHAR) AS entity_id,p.payment_type AS entity_name,
            p.order_id,p.payment_sequence,p.payment_type,p.payment_installments,p.payment_value
            FROM order_payments p JOIN scoped_orders o ON o.order_id=p.order_id ORDER BY p.order_id,p.payment_sequence"""
    else:
        sql = """SELECT r.review_id AS entity_id,r.review_id AS entity_name,r.order_id,r.review_score,r.review_title,r.review_message,r.review_created_at
            FROM order_reviews r JOIN scoped_orders o ON o.order_id=r.order_id ORDER BY r.order_id,r.review_id"""
    return CompiledQuery(prefix + " " + sql, base.parameters)


def diagnose(plan: AnalysisPlan) -> CompiledQuery:
    if plan.diagnosis in {"reconciliation", "quality"}:
        base = details(plan.model_copy(update={"detail_level": "orders"}))
        if plan.diagnosis == "reconciliation":
            threshold = plan.diagnostic_threshold if plan.diagnostic_threshold is not None else .01
            sql = f"""WITH order_details AS ({base.sql}) SELECT entity_id,entity_name,order_id,order_sales,order_freight,payment_amount,
                payment_amount-(order_sales+order_freight) AS difference
                FROM order_details WHERE payment_amount IS NULL OR order_sales IS NULL OR ABS(payment_amount-order_sales-order_freight)>?
                ORDER BY ABS(payment_amount-order_sales-order_freight) DESC NULLS FIRST,entity_id"""
            return CompiledQuery(sql, base.parameters + (threshold,))
        # 诊断使用同一订单范围，输出问题计数，包含零值。
        sql = f"""WITH selected AS ({base.sql})
            SELECT 'missing_timing' AS entity_id,'履约时间缺失' AS entity_name,COUNT(*) AS issue_count FROM selected WHERE order_status='delivered' AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)
            UNION ALL SELECT 'duplicate_reviews','一单多评价',COUNT(*) FROM (SELECT r.order_id FROM order_reviews r JOIN selected s ON s.order_id=r.order_id GROUP BY r.order_id HAVING COUNT(*)>1)
            UNION ALL SELECT 'negative_amount','负商品或运费',COUNT(*) FROM order_items i JOIN selected s ON s.order_id=i.order_id WHERE i.price<0 OR i.freight_value<0
            UNION ALL SELECT 'invalid_date','日期先后异常',COUNT(*) FROM selected WHERE delivered_at<purchase_at OR approved_at<purchase_at OR shipped_at<approved_at OR delivered_at<shipped_at OR estimated_delivery_at<purchase_at
            ORDER BY entity_id"""
        return CompiledQuery(sql, base.parameters)
    if plan.diagnosis == "sales_spike":
        base = aggregate(plan.model_copy(update={"intent": "aggregate", "dimension": "month", "metric_ids": ["sales"], "min_group_count": 1}))
        threshold = plan.diagnostic_threshold if plan.diagnostic_threshold is not None else .3
        # 日期相邻才称为环比；缺月不会跳过后把两个月误作相邻。
        sql = f"""WITH monthly AS ({base.sql}), series AS (
            SELECT *,LAG(sales) OVER (ORDER BY entity_id) AS previous_sales,LAG(entity_id) OVER (ORDER BY entity_id) AS previous_month FROM monthly
        ), changes AS (SELECT *,CAST(sales-previous_sales AS DOUBLE)/NULLIF(previous_sales,0) AS change_rate FROM series
            WHERE DATE_DIFF('month',CAST(previous_month||'-01' AS DATE),CAST(entity_id||'-01' AS DATE))=1)
        SELECT * FROM changes WHERE ABS(change_rate)>=? ORDER BY entity_id"""
        return CompiledQuery(sql, base.parameters + (threshold,))
    base = aggregate(plan.model_copy(update={"intent": "aggregate", "dimension": "seller", "metric_ids": ["delay_rate", "order_count"]}))
    threshold = plan.diagnostic_threshold if plan.diagnostic_threshold is not None else 2.0
    sql = f"""WITH sellers AS ({base.sql}), stats AS (SELECT *,AVG(delay_rate) OVER () AS mean_rate,STDDEV_POP(delay_rate) OVER () AS std_rate FROM sellers),
        scored AS (SELECT *, (delay_rate-mean_rate)/NULLIF(std_rate,0) AS z_score FROM stats)
        SELECT * FROM scored WHERE z_score>=? ORDER BY z_score DESC,entity_id"""
    return CompiledQuery(sql, base.parameters + (threshold,))


def compile_advanced(plan: AnalysisPlan) -> CompiledQuery:
    if plan.intent in {"compare", "contribution"}:
        return comparison(plan)
    if plan.intent == "details":
        return details(plan)
    if plan.intent == "diagnose":
        return diagnose(plan)
    return aggregate(plan)


def audit_advanced(plan: AnalysisPlan, compiled: CompiledQuery):
    """扩展计划也只能执行其编译产物；各类业务表达式另有原始数据回归验证。"""
    from app.sql_audit import require
    expected = compile_advanced(plan)
    require(compiled.parameters == expected.parameters, "扩展期间、状态或筛选参数")
    # 比较AST覆盖整条查询，包括关联、预聚合、分母、诊断门槛及排序。
    require(sqlglot.parse_one(compiled.sql, read="duckdb") == sqlglot.parse_one(expected.sql, read="duckdb"), "扩展查询结构与已登记计划不一致")
