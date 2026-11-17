"""订单范围与订单—分组事实；NULL 归属在聚合前保留。"""
from app.models import AnalysisPlan
from app.predicates import ITEM_FIELDS, ORDER_FIELDS, predicate_sql
from app.sql_compiler import CompiledQuery
from app.quality import exact_sum
from app.calculation import LEGACY_CONTEXT, exclusion_cte, eligible_sql
from app.grouping import dimensions, entity_sql, joint_projection, result_columns, KEY_COLUMNS


def scope_ctes(plan, context=LEGACY_CONTEXT):
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
    if context.excludes_orders:
        ctes.insert(0, exclusion_cte(context))
    return ctes, params


def quality_json(known, value_unknown, membership, affected, known_count, missing="0", *,
                 excluded="0", excluded_candidates="0", eligible="0", basis="full_population", group_uncertain="FALSE"):
    unresolved = f"(({value_unknown})+({membership})+({missing}))"
    incomplete = f"({unresolved}+({excluded})+({excluded_candidates}))"
    # DuckDB JSON can round a wide DECIMAL through floating point. Keep its
    # exact text at this boundary and decode it as Decimal on the host.
    return (f"json_object('known_value',CAST(({known}) AS VARCHAR),'known_count',{known_count},"
            f"'value_unknown_count',{value_unknown},'membership_unknown_count',{membership},"
            f"'missing_record_count',{missing},'affected_order_count',{affected},"
            f"'amount_basis','{basis}','policy_eligible_order_count',{eligible},"
            f"'excluded_order_count',{excluded},'excluded_candidate_order_count',{excluded_candidates},"
            f"'population_complete',({incomplete}=0),"
            f"'group_eligibility',CASE WHEN {group_uncertain} THEN 'uncertain' ELSE 'confirmed' END,"
            f"'calculation_status',CASE WHEN {unresolved}>0 THEN 'insufficient_data' "
            f"WHEN ({known}) IS NULL THEN 'undefined' ELSE 'determined' END,"
            f"'reason_codes',concat_ws(',',CASE WHEN ({value_unknown})>0 THEN 'value_unconfirmed' END,"
            f"CASE WHEN ({membership})>0 THEN 'membership_unconfirmed' END,CASE WHEN ({missing})>0 THEN 'missing_record' END,"
            f"CASE WHEN ({excluded})+({excluded_candidates})>0 THEN 'order_price_conflict_excluded' END),"
            f"'evidence_ref','quality_issues',"
            f"'completeness',CASE WHEN {incomplete}=0 THEN 'complete' "
            f"WHEN ({known_count})>0 THEN 'partial' ELSE 'unknown' END)")


def aggregate(plan: AnalysisPlan, context=LEGACY_CONTEXT) -> CompiledQuery:
    ctes, params = scope_ctes(plan, context)
    dims = dimensions(plan)
    grouped_items = bool(set(dims) & {"category", "seller"})
    item_columns = [c for d in dims if d in {"category", "seller"} for c in
                    (["category_id", "category_name_zh"] if d == "category" else ["seller_id", "seller_name"])]
    entity_columns = ",".join(item_columns) + "," if item_columns else ""
    entity_group = "," + ",".join(item_columns) if item_columns else ""
    ctes.append(f"""entity_items AS (SELECT order_id,{entity_columns}
        COALESCE(SUM(price) FILTER (WHERE item_match),0) AS sales_known,
        COUNT(price) FILTER (WHERE item_match) AS sales_known_count,
        COUNT(*) FILTER (WHERE item_match AND price IS NULL) AS sales_unknown,
        COALESCE(SUM(freight_value) FILTER (WHERE item_match),0) AS freight_known,
        COUNT(freight_value) FILTER (WHERE item_match) AS freight_known_count,
        COUNT(*) FILTER (WHERE item_match AND freight_value IS NULL) AS freight_unknown,
        COUNT(*) FILTER (WHERE item_match IS NULL) AS item_unknown,
        CASE WHEN COUNT(*) FILTER (WHERE item_match)>0 THEN TRUE
             WHEN COUNT(*) FILTER (WHERE item_match IS NULL)>0 THEN NULL ELSE FALSE END AS group_match
        FROM items GROUP BY order_id{entity_group})""")
    ctes.append(f"""payments AS (SELECT order_id,SUM(payment_value) AS payment_known,
        COUNT(payment_value) AS payment_known_count, COUNT(*)-COUNT(payment_value) AS payment_unknown,
        {exact_sum('payment_value')} AS payment_amount FROM order_payments GROUP BY order_id)""")
    key, name = entity_sql(plan)
    entity = ",".join([f"{key} AS entity_id", f"{name} AS entity_name", *joint_projection(plan)])
    match = "o.scope_match AND i.group_match" if grouped_items else "o.scope_match"
    reviews = bool(set(plan.metric_ids) & {"avg_review", "low_score_rate"})
    ctes.append(f"""facts AS (SELECT o.*,{entity},({match}) AS match_state,
        COALESCE(i.sales_known,0) AS sales_known,COALESCE(i.sales_known_count,0) AS sales_known_count,
        COALESCE(i.sales_unknown,0) AS sales_unknown,COALESCE(i.item_unknown,0) AS item_unknown,
        COALESCE(i.freight_known,0) AS freight_known,COALESCE(i.freight_known_count,0) AS freight_known_count,
        COALESCE(i.freight_unknown,0) AS freight_unknown,
        {eligible_sql(context)} AS amount_eligible,
        pay.payment_known,pay.payment_known_count,pay.payment_unknown,(pay.order_id IS NULL) AS payment_missing
        {',r.review_score' if reviews else ''}
        FROM scoped_orders o {'JOIN' if grouped_items else 'LEFT JOIN'} entity_items i ON i.order_id=o.order_id
        LEFT JOIN payments pay ON pay.order_id=o.order_id
        {'LEFT JOIN order_reviews r ON r.order_id=o.order_id' if reviews else ''})""")
    group_columns = ["entity_id", "entity_name", *result_columns(plan)]
    group = "GROUP BY " + ",".join(group_columns) if dims else ""
    projection = ",".join(group_columns) + "," if group else ""
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
            missing = excluded = excluded_candidates = eligible = "0"
            basis = "full_population"
            if metric in {"sales", "freight", "payment_amount"}:
                field = "payment" if metric == "payment_amount" else metric
                known = f"COALESCE(SUM({field}_known) FILTER (WHERE match_state),0)"
                count = f"COALESCE(SUM({field}_known_count) FILTER (WHERE match_state),0)"
                unknown = f"COALESCE(SUM({field}_unknown) FILTER (WHERE match_state),0)"
                membership = f"({u}+COALESCE(SUM(item_unknown) FILTER (WHERE match_state),0))" if metric in {"sales", "freight"} else u
                missing = "COUNT(*) FILTER (WHERE match_state AND payment_missing)" if metric == "payment_amount" else "0"
                affected = f"COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL OR {field}_unknown>0" + (" OR item_unknown>0)" if metric in {"sales", "freight"} else " OR payment_missing)")
                if metric in {"sales", "freight"} and context.excludes_orders:
                    basis = "policy_eligible"
                    known = f"COALESCE(SUM({field}_known) FILTER (WHERE match_state AND amount_eligible),0)"
                    count = f"COALESCE(SUM({field}_known_count) FILTER (WHERE match_state AND amount_eligible),0)"
                    unknown = f"COALESCE(SUM({field}_unknown) FILTER (WHERE match_state AND amount_eligible),0)"
                    membership = "(COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL AND amount_eligible)+COALESCE(SUM(item_unknown) FILTER (WHERE match_state AND amount_eligible),0))"
                    excluded = "COUNT(DISTINCT order_id) FILTER (WHERE match_state AND NOT amount_eligible)"
                    excluded_candidates = "COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL AND NOT amount_eligible)"
                    eligible = "COUNT(DISTINCT order_id) FILTER (WHERE match_state AND amount_eligible)"
                    affected = f"COUNT(DISTINCT order_id) FILTER (WHERE match_state IS NULL OR NOT amount_eligible OR {field}_unknown>0 OR item_unknown>0)"
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
            qualities.append(f"'{metric}'," + quality_json(known, unknown, membership, affected, count, missing, excluded=excluded, excluded_candidates=excluded_candidates, eligible=eligible, basis=basis, group_uncertain=f'{n}<{plan.min_group_count} AND {u}>0' if group else 'FALSE'))
        if "delay_rate" in plan.metric_ids:
            expressions += [f"{n} AS sample_orders", "COUNT(*) FILTER (WHERE match_state AND delivered_at>estimated_delivery_at) AS delayed_orders",
                            "COUNT(*) FILTER (WHERE match_state AND (delivered_at IS NULL OR estimated_delivery_at IS NULL)) AS missing_timing_orders"]
        if reviews:
            expressions.append("COUNT(review_score) FILTER (WHERE match_state) AS reviewed_orders")
    expressions.append(f"json_object({','.join(qualities)}) AS __quality")
    if source == "facts":
        expressions += [f"{n} AS __orders", f"{u} AS __uncertain_orders"]
    ctes.append(f"aggregated AS (SELECT {projection}{','.join(expressions)} FROM {source} {group})")
    threshold = f"(__orders+__uncertain_orders>={plan.min_group_count})" if group and source == "facts" else "TRUE"
    exclude = " EXCLUDE (__orders,__uncertain_orders)" if source == "facts" else ""
    if plan.intent == "rank":
        metric = plan.metric_ids[0]
        # 门槛只排除确定不合格者；在 Top N 前检查整个排名分区的未知候选。
        uncertain = f"({metric} IS NULL AND json_extract(__quality,'$.{metric}.completeness')<>'\"complete\"')"
        if source == "facts":
            uncertain += f" OR __orders<{plan.min_group_count}"
        partition = f"PARTITION BY {KEY_COLUMNS[plan.rank_within]} " if plan.rank_within else ""
        ctes.append(f"ranked AS (SELECT *,CASE WHEN COUNT(*) FILTER (WHERE {uncertain}) OVER ({partition})=0 THEN RANK() OVER ({partition}ORDER BY {metric} DESC NULLS LAST) END AS rank_no FROM aggregated WHERE {threshold})")
        body = f"SELECT *{exclude} FROM ranked WHERE rank_no<={plan.top_n} OR rank_no IS NULL ORDER BY {metric} DESC NULLS LAST,entity_id"
    else:
        body = f"SELECT *{exclude} FROM aggregated WHERE {threshold}" + (" ORDER BY entity_id" if group else "")
    return CompiledQuery("WITH " + ",\n".join(ctes) + "\n" + body, tuple(params))
