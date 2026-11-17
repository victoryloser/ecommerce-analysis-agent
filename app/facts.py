"""订单范围与订单—分组事实；NULL 归属在聚合前保留。"""
from app.models import AnalysisPlan
from app.predicates import ITEM_FIELDS, ORDER_FIELDS, predicate_sql
from app.sql_compiler import CompiledQuery
from app.quality import exact_sum


def scope_ctes(plan):
    params = [*plan.order_statuses, plan.time_range.start, plan.time_range.end]
    conditions = [f"COALESCE((o.order_status IN ({','.join('?' for _ in plan.order_statuses)}) AND o.{plan.time_field}>=? AND o.{plan.time_field}<?),FALSE)"]
    for f in plan.filters:
        if f.field == "customer_state":
            conditions.append(f"c.customer_state IN ({','.join('?' for _ in f.values)})")
            params.extend(f.values)
    order_sql, values = predicate_sql(plan.order_where, ORDER_FIELDS)
    params.extend(values)
    if order_sql:
        conditions.append(f"({order_sql})")
    if plan.selected_order_id:
        conditions.append("o.order_id=?")
        params.append(plan.selected_order_id)
    item_conditions = []
    for f in plan.filters:
        if f.field != "customer_state":
            item_conditions.append(f"{ITEM_FIELDS[f.field]} IN ({','.join('?' for _ in f.values)})")
            params.extend(f.values)
    item_sql, values = predicate_sql(plan.item_where, ITEM_FIELDS)
    params.extend(values)
    if item_sql:
        item_conditions.append(f"({item_sql})")
    item_match = " AND ".join(item_conditions) or "TRUE"
    ctes = [f"""order_candidates AS (SELECT o.*,c.customer_unique_id,c.customer_state,
        ({' AND '.join(conditions)}) AS order_match FROM orders o JOIN customers c ON c.customer_id=o.customer_id)""",
        "eligible_orders AS (SELECT * FROM order_candidates WHERE order_match IS NOT FALSE)",
        f"""item_candidates AS (SELECT i.*,p.category_id,cat.category_name_zh,s.seller_name,
        ({item_match}) AS item_match FROM order_items i JOIN eligible_orders o ON o.order_id=i.order_id
        JOIN products p ON p.product_id=i.product_id JOIN categories cat ON cat.category_id=p.category_id
        JOIN sellers s ON s.seller_id=i.seller_id)""",
        "items AS (SELECT * FROM item_candidates WHERE item_match IS NOT FALSE)"]
    exists = """(SELECT CASE WHEN COUNT(*) FILTER (WHERE i.item_match)>0 THEN TRUE
        WHEN COUNT(*) FILTER (WHERE i.item_match IS NULL)>0 THEN NULL ELSE FALSE END
        FROM items i WHERE i.order_id=o.order_id)""" if item_conditions else "TRUE"
    ctes += [f"scoped_candidates AS (SELECT o.*,order_match AND {exists} AS scope_match FROM eligible_orders o)",
             "scoped_orders AS (SELECT * FROM scoped_candidates WHERE scope_match IS NOT FALSE)"]
    return ctes, params


def quality_json(known, value_unknown, membership, affected, known_count, missing="0"):
    return (f"json_object('known_value',{known},'known_count',{known_count},"
            f"'value_unknown_count',{value_unknown},'membership_unknown_count',{membership},"
            f"'missing_record_count',{missing},'affected_order_count',{affected},"
            f"'reason_codes',concat_ws(',',CASE WHEN ({value_unknown})>0 THEN 'value_unconfirmed' END,"
            f"CASE WHEN ({membership})>0 THEN 'membership_unconfirmed' END,CASE WHEN ({missing})>0 THEN 'missing_record' END),"
            f"'evidence_ref','quality_issues',"
            f"'completeness',CASE WHEN ({value_unknown})+({membership})+({missing})=0 THEN 'complete' "
            f"WHEN ({known_count})>0 THEN 'partial' ELSE 'unknown' END)")


def aggregate(plan: AnalysisPlan) -> CompiledQuery:
    ctes, params = scope_ctes(plan)
    grouped_items = plan.dimension in {"category", "seller"}
    entity_columns = ("category_id AS entity_id,category_name_zh AS entity_name," if plan.dimension == "category"
                      else "seller_id AS entity_id,seller_name AS entity_name,") if grouped_items else ""
    entity_group = ",entity_id,entity_name" if grouped_items else ""
    ctes.append(f"""entity_items AS (SELECT order_id,{entity_columns}
        COALESCE(SUM(price) FILTER (WHERE item_match),0) AS sales_known,
        COUNT(price) FILTER (WHERE item_match) AS sales_known_count,
        COUNT(*) FILTER (WHERE item_match AND price IS NULL) AS sales_unknown,
        COUNT(*) FILTER (WHERE item_match IS NULL) AS item_unknown,
        CASE WHEN COUNT(*) FILTER (WHERE item_match)>0 THEN TRUE
             WHEN COUNT(*) FILTER (WHERE item_match IS NULL)>0 THEN NULL ELSE FALSE END AS group_match
        FROM items GROUP BY order_id{entity_group})""")
    ctes.append(f"""payments AS (SELECT order_id,SUM(payment_value) AS payment_known,
        COUNT(payment_value) AS payment_known_count, COUNT(*)-COUNT(payment_value) AS payment_unknown,
        {exact_sum('payment_value')} AS payment_amount FROM order_payments GROUP BY order_id)""")
    entity = "i.entity_id,i.entity_name" if grouped_items else {
        "total": "'all' AS entity_id,'全站' AS entity_name",
        "month": f"strftime(o.{plan.time_field},'%Y-%m') AS entity_id,strftime(o.{plan.time_field},'%Y-%m') AS entity_name",
        "customer_state": "o.customer_state AS entity_id,o.customer_state AS entity_name",
        "delivery_status": "CASE WHEN o.delivered_at IS NULL OR o.estimated_delivery_at IS NULL THEN 'unknown' WHEN o.delivered_at>o.estimated_delivery_at THEN 'delayed' ELSE 'on_time' END AS entity_id,CASE WHEN o.delivered_at IS NULL OR o.estimated_delivery_at IS NULL THEN '时间缺失' WHEN o.delivered_at>o.estimated_delivery_at THEN '延迟' ELSE '准时' END AS entity_name",
    }[plan.dimension]
    match = "o.scope_match AND i.group_match" if grouped_items else "o.scope_match"
    reviews = bool(set(plan.metric_ids) & {"avg_review", "low_score_rate"})
    ctes.append(f"""facts AS (SELECT o.*,{entity},({match}) AS match_state,
        COALESCE(i.sales_known,0) AS sales_known,COALESCE(i.sales_known_count,0) AS sales_known_count,
        COALESCE(i.sales_unknown,0) AS sales_unknown,COALESCE(i.item_unknown,0) AS item_unknown,
        pay.payment_known,pay.payment_known_count,pay.payment_unknown,(pay.order_id IS NULL) AS payment_missing
        {',r.review_score' if reviews else ''}
        FROM scoped_orders o {'JOIN' if grouped_items else 'LEFT JOIN'} entity_items i ON i.order_id=o.order_id
        LEFT JOIN payments pay ON pay.order_id=o.order_id
        {'LEFT JOIN order_reviews r ON r.order_id=o.order_id' if reviews else ''})""")
    group = "GROUP BY entity_id,entity_name" if plan.dimension != "total" else ""
    projection = "entity_id,entity_name," if group else ""
    n = "COUNT(DISTINCT order_id) FILTER (WHERE match_state)"
    u = "COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL)"
    expressions, qualities = [], []
    if plan.metric_ids == ["repeat_customers"]:
        ctes.append(f"customer_orders AS (SELECT {projection}customer_unique_id,{n} AS n,{u} AS u FROM facts GROUP BY {projection}customer_unique_id)")
        known = "COUNT(*) FILTER (WHERE n>=2)"
        unknown = "COUNT(*) FILTER (WHERE n<2 AND n+u>=2)"
        expressions.append(f"CASE WHEN {unknown}=0 THEN {known} END AS repeat_customers")
        qualities.append("'repeat_customers'," + quality_json(known, "0", unknown, unknown, known))
        source = "customer_orders"
    else:
        source = "facts"
        for metric in plan.metric_ids:
            missing = "0"
            if metric in {"sales", "payment_amount"}:
                field = "sales" if metric == "sales" else "payment"
                known = f"COALESCE(SUM({field}_known) FILTER (WHERE match_state),0)"
                count = f"COALESCE(SUM({field}_known_count) FILTER (WHERE match_state),0)"
                unknown = f"COALESCE(SUM({field}_unknown) FILTER (WHERE match_state),0)"
                membership = f"({u}+COALESCE(SUM(item_unknown) FILTER (WHERE match_state),0))" if metric == "sales" else u
                missing = "COUNT(*) FILTER (WHERE match_state AND payment_missing)" if metric == "payment_amount" else "0"
                affected = f"COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL OR {field}_unknown>0" + (" OR item_unknown>0)" if metric == "sales" else " OR payment_missing)")
                value = f"CASE WHEN {unknown}+{membership}+{missing}=0 THEN {known} END"
            elif metric == "order_count":
                known, count, unknown, membership, affected = n, n, "0", u, u
                value = f"CASE WHEN {u}=0 THEN {n} END"
            else:
                membership = u
                count = "COUNT(review_score) FILTER (WHERE match_state)" if reviews and metric != "delay_rate" else n
                if metric == "delay_rate":
                    unknown = "COUNT(*) FILTER (WHERE match_state AND (delivered_at IS NULL OR estimated_delivery_at IS NULL))"
                    known = f"CAST(COUNT(*) FILTER (WHERE match_state AND delivered_at>estimated_delivery_at) AS DOUBLE)/NULLIF({n},0)"
                elif metric == "avg_review":
                    unknown, known = "0", "AVG(review_score) FILTER (WHERE match_state)"
                else:
                    unknown, known = "0", f"CAST(COUNT(*) FILTER (WHERE match_state AND review_score<=2) AS DOUBLE)/NULLIF({count},0)"
                affected = f"({u}+{unknown})"
                value = f"CASE WHEN {u}+{unknown}=0 THEN {known} END"
            expressions.append(f"{value} AS {metric}")
            qualities.append(f"'{metric}'," + quality_json(known, unknown, membership, affected, count, missing))
        if "delay_rate" in plan.metric_ids:
            expressions += [f"{n} AS sample_orders", "COUNT(*) FILTER (WHERE match_state AND delivered_at>estimated_delivery_at) AS delayed_orders",
                            "COUNT(*) FILTER (WHERE match_state AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)) AS missing_timing_orders"]
        if reviews:
            expressions.append("COUNT(review_score) FILTER (WHERE match_state) AS reviewed_orders")
    expressions.append(f"json_object({','.join(qualities)}) AS __quality")
    if source == "facts":
        expressions += [f"{n} AS __orders", f"{u} AS __uncertain_orders"]
    ctes.append(f"aggregated AS (SELECT {projection}{','.join(expressions)} FROM {source} {group})")
    threshold = f"(__orders>={plan.min_group_count} OR __uncertain_orders>0)" if group and source == "facts" else "TRUE"
    exclude = " EXCLUDE (__orders,__uncertain_orders)" if source == "facts" else ""
    if plan.intent == "rank":
        metric = plan.metric_ids[0]
        # 在门槛及 Top N 之前判断未知候选，不能让未知组消失后宣称冠军。
        uncertain = f"{metric} IS NULL AND json_extract(__quality,'$.{metric}.completeness')<>'\"complete\"'"
        ctes.append(f"ranked AS (SELECT *,CASE WHEN COUNT(*) FILTER (WHERE {uncertain}) OVER ()=0 THEN RANK() OVER (ORDER BY {metric} DESC NULLS LAST) END AS rank_no FROM aggregated WHERE {threshold})")
        body = f"SELECT *{exclude} FROM ranked WHERE rank_no<={plan.top_n} OR rank_no IS NULL ORDER BY {metric} DESC NULLS LAST,entity_id"
    else:
        body = f"SELECT *{exclude} FROM aggregated WHERE {threshold}" + (" ORDER BY entity_id" if group else "")
    return CompiledQuery("WITH " + ",\n".join(ctes) + "\n" + body, tuple(params))
