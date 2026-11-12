"""审计受控编译器的业务契约；不接受自由SQL，不调用模型修正SQL。"""
import sqlglot
from sqlglot import exp

from app.metadata import EXPRESSIONS
from app.models import AnalysisPlan
from app.sql_compiler import CompiledQuery


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(f"SQL口径审计失败：{message}")


def expression(text: str):
    return sqlglot.parse_one(text, read="duckdb")


def columns_in_group(select) -> list[str]:
    group = select.args.get("group")
    return [c.sql(dialect="duckdb") for c in group.expressions] if group else []


def audit_select(plan: AnalysisPlan, tree: exp.Select):
    ctes = {cte.alias: cte.this for cte in tree.args["with_"].expressions}
    eligible = ctes["eligible_orders"]
    conditions = ["o.order_status='delivered'", "o.purchase_at>=?", "o.purchase_at<?"]
    for filter_ in plan.filters:
        if filter_.field == "customer_state":
            conditions.append(f"c.customer_state IN ({','.join('?' for _ in filter_.values)})")
    require(eligible.args["where"].this == expression(" AND ".join(conditions)), "已送达状态、下单时间或过滤条件")
    require(eligible.args["from_"].this.name == "orders", "订单来源")
    joins = eligible.args["joins"]
    require(len(joins) == 1 and joins[0].this.name == "customers" and not joins[0].args.get("side")
            and joins[0].args["on"] == expression("c.customer_id=o.customer_id"), "客户关联路径")
    item_joins = {join.this.alias_or_name: join for join in ctes["items"].args["joins"]}
    require(ctes["items"].args["from_"].this.name == "order_items" and len(item_joins) == 4, "商品明细来源")
    for alias, table, on in [("o", "eligible_orders", "o.order_id=i.order_id"), ("p", "products", "p.product_id=i.product_id"),
                             ("cat", "categories", "cat.category_id=p.category_id"), ("s", "sellers", "s.seller_id=i.seller_id")]:
        join = item_joins[alias]
        require(join.this.name == table and join.args["on"] == expression(on) and not join.args.get("side"), "明细关联路径")
    item_conditions = []
    for filter_ in plan.filters:
        if filter_.field != "customer_state":
            field = {"seller_id": "i.seller_id", "category_id": "p.category_id"}[filter_.field]
            item_conditions.append(f"{field} IN ({','.join('?' for _ in filter_.values)})")
    where = ctes["items"].args.get("where")
    require((where.this if where else None) == (expression(" AND ".join(item_conditions)) if item_conditions else None), "明细实体过滤")
    scoped_where = ctes["scoped_orders"].args.get("where")
    require((scoped_where.this if scoped_where else None) == (expression("EXISTS (SELECT 1 FROM items i WHERE i.order_id=o.order_id)") if item_conditions else None), "订单与明细筛选范围")
    entity = ctes["entity_items"]
    expected_group = ["order_id"]
    if plan.dimension in {"seller", "category"}:
        expected_group += [f"{plan.dimension}_id", "seller_name" if plan.dimension == "seller" else "category_name_zh"]
    require(columns_in_group(entity) == expected_group, "商品预聚合粒度")
    require(next(c.this for c in entity.expressions if c.alias == "sales") == expression("SUM(price)"), "商品价款口径")
    facts = ctes["facts"]
    require(facts.args["from_"].this.name == "scoped_orders", "事实订单来源")
    fact_joins = {j.this.alias_or_name: j for j in facts.args["joins"]}
    expected_joins = [("i", "entity_items", "i.order_id=o.order_id", "" if plan.dimension in {"category", "seller"} else "LEFT")]
    if "payment_amount" in plan.metric_ids:
        expected_joins.append(("pay", "payments", "pay.order_id=o.order_id", "LEFT"))
    if set(plan.metric_ids) & {"avg_review", "low_score_rate"}:
        expected_joins.append(("r", "order_reviews", "r.order_id=o.order_id", "LEFT"))
    require(len(fact_joins) == len(expected_joins), "事实层关联基数")
    for alias, table, on, side in expected_joins:
        join = fact_joins[alias]
        require(join.this.name == table and join.args["on"] == expression(on) and (join.args.get("side") or "") == side, "事实层关联路径")
    if "payment_amount" in plan.metric_ids:
        payment = ctes["payments"]
        require(columns_in_group(payment) == ["order_id"] and payment.args["from_"].this.name == "order_payments", "支付必须先按订单汇总")
        require(payment.expressions[1].this == expression("SUM(payment_value)"), "支付金额来源")
    aggregated = ctes["aggregated"]
    expressions = {c.alias: c.this for c in aggregated.expressions if isinstance(c, exp.Alias)}
    for metric in plan.metric_ids:
        require(expressions[metric] == expression(EXPRESSIONS[metric]), f"{metric}的分子、分母、去重或缺失口径")
    expected = [] if plan.dimension == "total" else ["entity_id", "entity_name"]
    require(columns_in_group(aggregated) == expected, "最终分组粒度")
    if plan.metric_ids == ["repeat_customers"]:
        customers = ctes["customer_orders"]
        require(columns_in_group(customers) == expected + ["customer_unique_id"], "复购自然客户标识")
        require(customers.expressions[-1].this == expression("COUNT(DISTINCT order_id)"), "复购订单去重")
    elif plan.dimension != "total":
        require(aggregated.args["having"].this == expression(f"COUNT(DISTINCT order_id)>={plan.min_group_count}"), "订单门槛")
    if plan.intent == "rank":
        rank = ctes["ranked"].expressions[-1].this
        require(isinstance(rank, exp.Window) and isinstance(rank.this, exp.Rank), "并列名次必须使用RANK")
        require(rank.args["order"].expressions[0] == expression(f"SELECT 1 ORDER BY {plan.metric_ids[0]} DESC NULLS LAST").args["order"].expressions[0], "排名指标和方向")
        require(tree.args["where"].this == expression(f"rank_no<={plan.top_n}"), "Top N范围")


def audit_query(plan: AnalysisPlan, compiled: CompiledQuery):
    from app.advanced import extended, audit_advanced
    if extended(plan):
        audit_advanced(plan, compiled)
        return
    tree = sqlglot.parse_one(compiled.sql, read="duckdb")
    require(len(list(tree.find_all(exp.Placeholder))) == len(compiled.parameters), "绑定参数数量")
    if plan.intent == "join_example":
        require(compiled.parameters == (plan.time_range.start, plan.time_range.end), "示例期间参数")
        ctes = {c.alias: c.this for c in tree.args["with_"].expressions}
        for name, table, amount in [("item_amounts", "order_items", "price"), ("payment_amounts", "order_payments", "payment_value")]:
            select = ctes[name]
            require(select.args["from_"].this.name == table and columns_in_group(select) == ["order_id"], "示例预聚合粒度")
            require(select.expressions[-1].this == expression(f"SUM({amount})"), "示例金额")
            require(select.expressions[1].this == expression("COUNT(*)"), "示例明细行数")
        projections = {c.alias if isinstance(c, exp.Alias) else c.name: c.this if isinstance(c, exp.Alias) else c for c in tree.expressions}
        expected = {"order_id": "o.order_id", "item_rows": "i.item_rows", "payment_rows": "p.payment_rows",
                    "joined_rows": "i.item_rows*p.payment_rows", "sales": "i.sales", "payment_amount": "p.payment_amount",
                    "inflated_sales": "i.sales*p.payment_rows", "inflated_payment": "p.payment_amount*i.item_rows"}
        require(projections == {key: expression(value) for key, value in expected.items()}, "示例关联放大公式")
        require(tree.args["where"].this == expression("o.order_status='delivered' AND o.purchase_at>=? AND o.purchase_at<? AND i.item_rows>1 AND p.payment_rows>1"), "示例范围与多行条件")
        require(tree.args["limit"].expression == exp.Literal.number(1), "示例行数")
        return
    periods = [plan.time_range, plan.comparison_range] if plan.intent == "compare" else [plan.time_range]
    parameters = []
    for period in periods:
        parameters += [period.start, period.end]
        parameters += [v for f in plan.filters if f.field == "customer_state" for v in f.values]
        parameters += [v for f in plan.filters if f.field != "customer_state" for v in f.values]
    require(tuple(parameters) == compiled.parameters, "期间和实体参数必须与计划一致")
    if plan.intent == "compare":
        metric = plan.metric_ids[0]
        required = {"current_value": f"c.{metric}", "previous_value": f"p.{metric}",
                    "absolute_change": f"c.{metric}-p.{metric}",
                    "change_rate": f"CAST(c.{metric}-p.{metric} AS DOUBLE)/NULLIF(p.{metric},0)"}
        require({c.alias: c.this for c in tree.expressions} == {key: expression(value) for key, value in required.items()}, "比较基期与变化率")
        audit_select(plan.model_copy(update={"intent": "aggregate", "comparison_range": None}), tree.args["from_"].this.this)
        audit_select(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None}), tree.args["joins"][0].this.this)
    else:
        audit_select(plan, tree)
