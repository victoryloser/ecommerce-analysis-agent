"""有明确粒度和口径的扩展编译：条件、分组比较、贡献、明细及诊断。"""
import sqlglot

from app.models import AnalysisPlan
from app.sql_compiler import CompiledQuery
from app.grouping import dimensions, result_columns, KEY_COLUMNS


def extended(plan: AnalysisPlan) -> bool:
    return (bool(plan.group_by) or plan.intent in {"contribution", "details", "diagnose"} or plan.time_field != "purchase_at"
            or plan.order_statuses != ["delivered"] or plan.order_where is not None or plan.item_where is not None
            or plan.selected_order_id is not None or (plan.intent == "compare" and (plan.dimension != "total" or len(plan.metric_ids) != 1)))


def aggregate(plan: AnalysisPlan) -> CompiledQuery:
    from app.facts import aggregate as compile_facts
    return compile_facts(plan)


def comparison(plan: AnalysisPlan) -> CompiledQuery:
    metrics = list(plan.metric_ids)
    if plan.min_group_count > 1 and "order_count" not in metrics:
        metrics.append("order_count")
    current = aggregate(plan.model_copy(update={"intent": "aggregate", "comparison_range": None, "min_group_count": 1, "metric_ids": metrics}))
    previous = aggregate(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None, "min_group_count": 1, "metric_ids": metrics}))
    grouping = bool(dimensions(plan))
    projections = ["COALESCE(c.entity_id,p.entity_id) AS entity_id", "COALESCE(c.entity_name,p.entity_name) AS entity_name"] if grouping else []
    projections += [f"COALESCE(c.{col},p.{col}) AS {col}" for col in result_columns(plan)]
    additive = {"sales", "order_count", "payment_amount", "repeat_customers"}
    for metric in plan.metric_ids:
        c = f"CASE WHEN c.__quality IS NULL THEN 0 ELSE c.{metric} END" if grouping and metric in additive else f"c.{metric}"
        p = f"CASE WHEN p.__quality IS NULL THEN 0 ELSE p.{metric} END" if grouping and metric in additive else f"p.{metric}"
        projections += [f"{c} AS {metric}_current", f"{p} AS {metric}_previous",
                        f"{c}-{p} AS {metric}_change", f"CAST({c}-{p} AS DOUBLE)/NULLIF({p},0) AS {metric}_rate"]
    from app.facts import quality_json
    quality_fields = [f"'{m}_{role}',COALESCE(json_extract({alias}.__quality,'$.{m}'),{quality_json('0' if m in additive else 'NULL', '0', '0', '0', '0')})" for m in plan.metric_ids for role, alias in [("current", "c"), ("previous", "p")]]
    projections.append(f"json_object({','.join(quality_fields)}) AS __quality")
    if plan.min_group_count > 1:
        projections += ["CASE WHEN c.__quality IS NULL THEN 0 ELSE c.order_count END AS current_orders", "CASE WHEN p.__quality IS NULL THEN 0 ELSE p.order_count END AS previous_orders"]
        possible = [f"(COALESCE(CAST(json_extract({side}.__quality,'$.order_count.known_value') AS BIGINT),0)+COALESCE(CAST(json_extract({side}.__quality,'$.order_count.membership_unknown_count') AS BIGINT),0)>={plan.min_group_count})" for side in ("c", "p")]
        projections.append("(" + " AND ".join(possible) + ") AS __eligible")
    keys = [KEY_COLUMNS[d] for d in dimensions(plan)] if plan.group_by else ["entity_id"]
    join = "FULL OUTER JOIN previous p ON " + " AND ".join(f"c.{key} IS NOT DISTINCT FROM p.{key}" for key in keys) if grouping else "CROSS JOIN previous p"
    sql = f"WITH current AS ({current.sql}), previous AS ({previous.sql}), changes AS (SELECT {','.join(projections)} FROM current c {join}) "
    threshold = " WHERE __eligible" if plan.min_group_count > 1 else ""
    excluded = " EXCLUDE (__eligible)" if plan.min_group_count > 1 else ""
    if plan.intent == "contribution":
        metric = plan.metric_ids[0]
        total = f"CASE WHEN COUNT(*) OVER ()=COUNT({metric}_change) OVER () THEN SUM({metric}_change) OVER () END"
        fields = [f"({total}) AS total_change", f"CAST({metric}_change AS DOUBLE)/NULLIF(({total}),0) AS contribution_rate"]
        partition = f"PARTITION BY {KEY_COLUMNS[plan.contribution_within]} " if plan.contribution_within else ""
        if partition:
            subtotal = f"CASE WHEN COUNT(*) OVER ({partition})=COUNT({metric}_change) OVER ({partition}) THEN SUM({metric}_change) OVER ({partition}) END"
            fields += [f"({subtotal}) AS parent_change", f"CAST({metric}_change AS DOUBLE)/NULLIF(({subtotal}),0) AS parent_contribution_rate"]
        direction = "ASC" if plan.contribution_direction == "decrease" else "DESC"
        sql += f", contributions AS (SELECT *,{','.join(fields)} FROM changes), eligible AS (SELECT *{excluded} FROM contributions{threshold}), ranked AS (SELECT *,CASE WHEN COUNT(*) OVER ({partition})=COUNT({metric}_change) OVER ({partition}) THEN RANK() OVER ({partition}ORDER BY {metric}_change {direction} NULLS LAST) END AS __rank FROM eligible) SELECT * EXCLUDE (__rank) FROM ranked WHERE __rank<={plan.top_n} OR __rank IS NULL ORDER BY {metric}_change {direction} NULLS LAST,entity_id"
    else:
        sql += f"SELECT *{excluded} FROM changes{threshold}"
        sql += " ORDER BY entity_id" if grouping else ""
    return CompiledQuery(sql, current.parameters + previous.parameters)


def details(plan: AnalysisPlan) -> CompiledQuery:
    from app.facts import scope_ctes
    ctes, parameters = scope_ctes(plan)
    prefix = "WITH " + ",".join(ctes)
    if plan.detail_level == "orders":
        sql = """SELECT o.order_id AS entity_id,o.order_id AS entity_name,o.order_id,o.order_status,o.purchase_at,o.delivered_at,
            o.approved_at,o.shipped_at,o.estimated_delivery_at,c.customer_unique_id,c.customer_state,o.scope_match AS membership_confirmed,
            (SELECT CASE WHEN COUNT(*)=COUNT(x.price) THEN SUM(x.price) END FROM order_items x WHERE x.order_id=o.order_id) AS order_sales,
            (SELECT SUM(x.price) FROM order_items x WHERE x.order_id=o.order_id) AS order_sales_known,
            (SELECT COUNT(*)-COUNT(x.price) FROM order_items x WHERE x.order_id=o.order_id) AS order_sales_unknown,
            (SELECT CASE WHEN COUNT(*)=COUNT(x.freight_value) THEN SUM(x.freight_value) END FROM order_items x WHERE x.order_id=o.order_id) AS order_freight,
            (SELECT SUM(x.freight_value) FROM order_items x WHERE x.order_id=o.order_id) AS order_freight_known,
            (SELECT COUNT(*)-COUNT(x.freight_value) FROM order_items x WHERE x.order_id=o.order_id) AS order_freight_unknown,
            (SELECT CASE WHEN COUNT(*)=COUNT(x.payment_value) THEN SUM(x.payment_value) END FROM order_payments x WHERE x.order_id=o.order_id) AS payment_amount,
            (SELECT SUM(x.payment_value) FROM order_payments x WHERE x.order_id=o.order_id) AS payment_known,
            (SELECT COUNT(*)-COUNT(x.payment_value) FROM order_payments x WHERE x.order_id=o.order_id) AS payment_unknown,
            NOT EXISTS (SELECT 1 FROM order_payments x WHERE x.order_id=o.order_id) AS payment_missing,
            (SELECT CASE WHEN COUNT(*)=1 THEN MAX(x.review_score) ELSE NULL END FROM order_reviews x WHERE x.order_id=o.order_id) AS review_score
            FROM scoped_orders o JOIN customers c ON c.customer_id=o.customer_id ORDER BY o.order_id"""
    elif plan.detail_level == "items":
        sql = """SELECT i.order_id||':'||CAST(i.order_item_id AS VARCHAR) AS entity_id,i.product_id AS entity_name,
            i.order_id,i.order_item_id,i.product_id,p.product_name,i.category_id,i.category_name_zh,i.seller_id,i.seller_name,i.price,i.freight_value,
            (o.scope_match AND i.item_match) AS membership_confirmed
            FROM items i JOIN scoped_orders o ON o.order_id=i.order_id JOIN products p ON p.product_id=i.product_id ORDER BY i.order_id,i.order_item_id"""
    elif plan.detail_level == "payments":
        sql = """SELECT p.order_id||':'||CAST(p.payment_sequence AS VARCHAR) AS entity_id,p.payment_type AS entity_name,
            p.order_id,p.payment_sequence,p.payment_type,p.payment_installments,p.payment_value,o.scope_match AS membership_confirmed
            FROM order_payments p JOIN scoped_orders o ON o.order_id=p.order_id ORDER BY p.order_id,p.payment_sequence"""
    else:
        sql = """SELECT r.review_id AS entity_id,r.review_id AS entity_name,r.order_id,r.review_score,r.review_title,r.review_message,r.review_created_at,o.scope_match AS membership_confirmed
            FROM order_reviews r JOIN scoped_orders o ON o.order_id=r.order_id ORDER BY r.order_id,r.review_id"""
    return CompiledQuery(prefix + " " + sql, tuple(parameters))


def diagnose(plan: AnalysisPlan) -> CompiledQuery:
    if plan.diagnosis in {"reconciliation", "quality"}:
        base = details(plan.model_copy(update={"detail_level": "orders"}))
        if plan.diagnosis == "reconciliation":
            threshold = plan.diagnostic_threshold if plan.diagnostic_threshold is not None else .01
            sql = f"""WITH order_details AS ({base.sql}) SELECT entity_id,entity_name,order_id,order_sales,order_freight,payment_amount,
                payment_amount-(order_sales+order_freight) AS difference
                FROM order_details WHERE payment_amount IS NULL OR order_sales IS NULL OR order_freight IS NULL OR ABS(payment_amount-order_sales-order_freight)>?
                ORDER BY ABS(payment_amount-order_sales-order_freight) DESC NULLS FIRST,entity_id"""
            return CompiledQuery(sql, base.parameters + (threshold,))
        # 诊断使用同一订单范围，输出问题计数，包含零值。
        sql = f"""WITH selected AS ({base.sql})
            SELECT 'missing_timing' AS entity_id,'履约时间缺失' AS entity_name,COUNT(*) AS issue_count FROM selected WHERE order_status='delivered' AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)
            UNION ALL SELECT 'duplicate_reviews','一单多评价',COUNT(*) FROM (SELECT r.order_id FROM order_reviews r JOIN selected s ON s.order_id=r.order_id GROUP BY r.order_id HAVING COUNT(*)>1)
            UNION ALL SELECT 'negative_amount','负商品或运费',COUNT(*) FROM order_items i JOIN selected s ON s.order_id=i.order_id WHERE i.price<0 OR i.freight_value<0
            UNION ALL SELECT 'invalid_date','日期先后异常',COUNT(*) FROM selected WHERE delivered_at<purchase_at OR approved_at<purchase_at OR shipped_at<approved_at OR delivered_at<shipped_at OR estimated_delivery_at<purchase_at
            UNION ALL SELECT q.reason,q.reason,COUNT(*) FROM quality_issues q JOIN selected s ON s.order_id=q.order_id GROUP BY q.reason
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
