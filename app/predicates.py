"""订单条件与商品明细条件分开定义，分别在对应粒度求值。"""
from app.models import Predicate
from app.quality import exact_sum

ORDER_FIELDS = {
    "order_id": "o.order_id", "customer_state": "c.customer_state", "order_status": "o.order_status",
    "delivery_status": "CASE WHEN o.delivered_at IS NULL OR o.estimated_delivery_at IS NULL THEN 'unknown' WHEN o.delivered_at>o.estimated_delivery_at THEN 'delayed' ELSE 'on_time' END",
    "order_sales": f"(SELECT {exact_sum('z.price')} FROM order_items z WHERE z.order_id=o.order_id)",
    "order_freight": f"(SELECT {exact_sum('z.freight_value')} FROM order_items z WHERE z.order_id=o.order_id)",
    "payment_amount": f"(SELECT {exact_sum('z.payment_value')} FROM order_payments z WHERE z.order_id=o.order_id)",
    "review_score": "(SELECT MAX(z.review_score) FROM order_reviews z WHERE z.order_id=o.order_id)",
}
ITEM_FIELDS = {"category_id": "p.category_id", "seller_id": "i.seller_id", "product_id": "i.product_id", "price": "i.price", "freight_value": "i.freight_value"}
NUMERIC = {"order_sales", "order_freight", "payment_amount", "review_score", "price", "freight_value"}


def predicate_sql(node: Predicate | None, fields: dict, depth: int = 0) -> tuple[str, list]:
    if node is None:
        return "", []
    if depth > 6:
        raise ValueError("筛选条件最多6层")
    parameters = []
    if node.children:
        if node.field is not None or node.values:
            raise ValueError("组合条件只填写children，叶子条件填写field")
        parts = []
        for child in node.children:
            sql, values = predicate_sql(child, fields, depth + 1)
            parts.append(f"({sql})")
            parameters.extend(values)
        sql = (" AND " if node.conjunction == "all" else " OR ").join(parts)
    else:
        if node.field not in fields:
            raise ValueError(f"当前筛选粒度不支持字段：{node.field}")
        column, operator, values = fields[node.field], node.operator, node.values
        if node.field in NUMERIC and any(not isinstance(v, (int, float)) for v in values):
            raise ValueError(f"{node.field}的筛选值必须是数字")
        if operator in {"is_null", "not_null"}:
            if values:
                raise ValueError("空值检查不使用values")
            sql = f"{column} IS {'NOT ' if operator == 'not_null' else ''}NULL"
        elif operator in {"in", "not_in"}:
            if not values:
                raise ValueError("IN/NOT IN需要至少一个值")
            sql = f"{column} {'NOT ' if operator == 'not_in' else ''}IN ({','.join('?' for _ in values)})"
            parameters.extend(values)
        elif operator == "between":
            if len(values) != 2 or values[0] > values[1]:
                raise ValueError("BETWEEN需要两个递增的边界值")
            sql = f"{column} BETWEEN ? AND ?"
            parameters.extend(values)
        else:
            if len(values) != 1:
                raise ValueError("比较条件需要一个值")
            symbol = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}[operator]
            sql = f"{column}{symbol}?"
            parameters.extend(values)
    if node.field == "review_score" and node.operator not in {"is_null", "not_null"}:
        sql = f"COALESCE(({sql}),FALSE)"
    return (f"NOT ({sql})" if node.negate else sql), parameters
