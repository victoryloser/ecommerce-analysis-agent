"""Audited, read-only quality and order inspections at explicit grains."""
from app.calculation import price_conflict_sql, eligible_sql
from app.facts import scope_ctes
from app.sql_compiler import CompiledQuery


def quality_query(plan, context):
    periods = [plan.time_range]
    if plan.quality_scope == "previous":
        periods = [plan.comparison_range]
    elif plan.quality_scope == "comparison_union" and plan.comparison_range:
        periods.append(plan.comparison_range)
    selections, parameters = [], []
    for period in periods:
        scoped = plan.model_copy(update={"time_range": period})
        ctes, params = scope_ctes(scoped, context)
        selections.append("WITH " + ",".join(ctes) + " SELECT order_id,scope_match FROM scoped_orders")
        parameters.extend(params)
    ctes = [f"period_{i} AS ({sql})" for i, sql in enumerate(selections)]
    ctes += ["members AS (SELECT order_id,CASE WHEN COUNT(*) FILTER (WHERE scope_match)>0 THEN TRUE ELSE NULL END AS membership_confirmed FROM (" +
             " UNION ALL ".join(f"SELECT * FROM period_{i}" for i in range(len(selections))) + ") scopes GROUP BY order_id)"]
    fragments = []
    for kind in dict.fromkeys(plan.quality_types):
        if kind == "missing_payment":
            fragments.append("""SELECT 'missing_payment' AS issue_type,m.order_id,m.order_id AS business_key,
                'order_payments' AS table_name,'payment_value' AS field_name,m.membership_confirmed,
                0::BIGINT AS extra_duplicate_rows,0::BIGINT AS candidate_count,
                NULL::VARCHAR AS source_rows,NULL::VARCHAR AS candidates
                FROM members m WHERE NOT EXISTS (SELECT 1 FROM order_payments p WHERE p.order_id=m.order_id)""")
        else:
            condition = "q.table_name='order_items' AND q.reason='exact_duplicate'" if kind == "exact_duplicate" else price_conflict_sql("q")
            if kind == "other":
                condition = f"NOT ((q.table_name='order_items' AND q.reason='exact_duplicate') OR ({price_conflict_sql('q')}) OR (q.table_name='order_payments' AND q.reason='missing_record'))"
            extra = "CAST(json_array_length(q.source_rows) AS BIGINT)-CAST(json_array_length(q.candidates) AS BIGINT)" if kind == "exact_duplicate" else "0::BIGINT"
            fragments.append(f"""SELECT '{kind}' AS issue_type,q.order_id,q.business_key,q.table_name,q.field_name,m.membership_confirmed,
                {extra} AS extra_duplicate_rows,CAST(json_array_length(q.candidates) AS BIGINT) AS candidate_count,
                q.source_rows,q.candidates FROM quality_issues q JOIN members m ON m.order_id=q.order_id WHERE {condition}""")
    ctes.append("problems AS (" + " UNION ALL ".join(fragments) + ")")
    if plan.diagnosis == "quality_summary":
        summaries = []
        for kind in [*dict.fromkeys(plan.quality_types), "all"]:
            where = "" if kind == "all" else f" WHERE issue_type='{kind}'"
            summaries.append(f"""SELECT '{kind}' AS issue_type,
                COUNT(DISTINCT business_key) FILTER (WHERE membership_confirmed) AS issue_key_count,
                COUNT(DISTINCT order_id) FILTER (WHERE membership_confirmed) AS affected_orders,
                COUNT(DISTINCT order_id) FILTER (WHERE membership_confirmed IS NULL) AS uncertain_orders,
                COALESCE(SUM(extra_duplicate_rows) FILTER (WHERE membership_confirmed),0) AS extra_duplicate_rows
                FROM problems{where}""")
        body = " UNION ALL ".join(summaries)
    else:
        body = "SELECT issue_type||':'||business_key AS entity_id,issue_type AS entity_name,* FROM problems ORDER BY issue_type,order_id,business_key"
    return CompiledQuery("WITH " + ",".join(ctes) + " " + body, tuple(parameters))


def order_candidate(plan, context):
    ctes, params = scope_ctes(plan, context)
    sql = "WITH " + ",".join(ctes) + f""" SELECT o.order_id AS entity_id,o.order_id AS entity_name,o.order_id,
        MIN(i.seller_id) AS seller_id,TRUE AS membership_confirmed
        FROM scoped_orders o JOIN items i ON i.order_id=o.order_id
        WHERE o.scope_match AND i.item_match AND {eligible_sql(context)}
        GROUP BY o.order_id ORDER BY o.order_id LIMIT 1"""
    return CompiledQuery(sql, tuple(params))


def order_audit(plan, context):
    from app.advanced import details
    # Selection has already bound an order. Inspection reads all its facts.
    clean = plan.model_copy(update={"filters": [], "order_where": None, "item_where": None, "detail_level": "orders"})
    base = details(clean, context)
    sql = f"""WITH header AS ({base.sql}) SELECT h.*,? AS selected_seller_id,
        (SELECT CASE WHEN COUNT(*)=COUNT(i.price) THEN SUM(i.price) END FROM order_items i
            WHERE i.order_id=h.order_id AND i.seller_id=?) AS seller_sales,
        (SELECT COUNT(*) FROM order_payments p WHERE p.order_id=h.order_id) AS payment_records
        FROM header h"""
    return CompiledQuery(sql, base.parameters + (plan.audit_seller_id, plan.audit_seller_id))
