"""审计受控编译器的业务契约；不接受自由SQL，不调用模型修正SQL。"""
import sqlglot
from sqlglot import exp

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


def audit_query(plan: AnalysisPlan, compiled: CompiledQuery):
    if plan.intent != "join_example":
        from app.sql_compiler import compile_query
        expected = compile_query(plan)
        require(compiled.parameters == expected.parameters, "期间、状态与筛选参数")
        require(expression(compiled.sql) == expression(expected.sql), "事实粒度、完整度与已登记计划结构")
        return
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
            require(select.expressions[-1].this == expression(f"CASE WHEN COUNT(*)=COUNT({amount}) THEN SUM({amount}) END"), "示例金额完整性")
            require(select.expressions[1].this == expression("COUNT(*)"), "示例明细行数")
        projections = {c.alias if isinstance(c, exp.Alias) else c.name: c.this if isinstance(c, exp.Alias) else c for c in tree.expressions}
        expected = {"order_id": "o.order_id", "item_rows": "i.item_rows", "payment_rows": "p.payment_rows",
                    "joined_rows": "i.item_rows*p.payment_rows", "sales": "i.sales", "payment_amount": "p.payment_amount",
                    "inflated_sales": "i.sales*p.payment_rows", "inflated_payment": "p.payment_amount*i.item_rows"}
        require(projections == {key: expression(value) for key, value in expected.items()}, "示例关联放大公式")
        require(tree.args["where"].this == expression("o.order_status='delivered' AND o.purchase_at>=? AND o.purchase_at<? AND i.item_rows>1 AND p.payment_rows>1"), "示例范围与多行条件")
        require(tree.args["limit"].expression == exp.Literal.number(1), "示例行数")
        return
