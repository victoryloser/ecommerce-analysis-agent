"""Field contract for order exports with optional payment and review records."""
from decimal import Decimal, InvalidOperation

from app.source import SCHEMA

TEMPLATE = "orders_items"
REQUIRED = {
    "orders": ("order_id", "purchase_at", "order_status"),
    "order_items": ("order_id", "order_item_id"),
    "order_payments": ("order_id", "payment_sequence", "payment_value"),
    "order_reviews": ("order_id", "review_id", "review_score"),
}
FIELDS = {**REQUIRED, "order_items": (*REQUIRED["order_items"], "price", "quantity", "freight_value",
                                     "category_id", "category_name_zh", "seller_id", "seller_name", "product_id")}
ITEM_SCHEMA = {**SCHEMA["order_items"], "order_item_id": "VARCHAR", "category_id": "VARCHAR",
               "category_name_zh": "VARCHAR", "seller_name": "VARCHAR"}
INPUT_HINTS = {"sales": "确认不含运费的行商品额，或映射单价与数量",
               "freight": "确认商品额口径并映射明细运费 freight_value",
               "payment_amount": "补充含支付序号和支付金额的支付表",
               "avg_review": "补充含评价 ID、订单号和评分的评价表",
               "low_score_rate": "补充含评价 ID、订单号和评分的评价表",
               "category": "在明细中映射品类 ID", "seller": "在明细中映射商家 ID",
               "repeat_customers": "使用含自然客户标识的完整九表模板",
               "delay_rate": "使用含实际及预计送达时间的完整九表模板",
               "customer_state": "使用含客户省份的完整九表模板"}


def contract(mapping):
    tables = mapping["tables"]
    if not {"orders", "order_items"} <= tables.keys() or tables.keys() - REQUIRED.keys():
        raise ValueError("轻量模板需要订单和明细，可追加支付和评价表")
    for table, spec in tables.items():
        columns = spec["columns"]
        if columns.keys() - set(FIELDS[table]) or not all(columns.get(key) for key in REQUIRED[table]):
            raise ValueError(f"{table} 需要字段 {', '.join(REQUIRED[table])}")
    item = tables["order_items"]["columns"]
    mode = mapping["amount_mode"]
    if mode not in {"line", "unit", "unknown"}:
        raise ValueError("请选择不含运费的行商品额、单价乘数量或金额待确认")
    if mode != "unknown" and (not item.get("price") or (mode == "unit" and not item.get("quantity"))):
        raise ValueError("请映射商品金额，按单价计算时还需映射数量")
    if mapping.get("currency") != "CNY":
        raise ValueError("当前模板支持人民币元，请提供同一币种的导出")
    statuses = mapping.get("status_map", {})
    if not isinstance(statuses, dict) or any(not isinstance(v, str) or not v.strip() for v in statuses.values()):
        raise ValueError("状态映射应为来源状态与标准状态的对应表")
    metrics = ["order_count"]
    if mode != "unknown":
        metrics.append("sales")
        if item.get("freight_value"):
            metrics.append("freight")
    if "order_payments" in tables:
        metrics.append("payment_amount")
    if "order_reviews" in tables:
        metrics.extend(("avg_review", "low_score_rate"))
    dimensions = ["total", "month", *(dim for dim in ("category", "seller") if item.get(dim + "_id"))]
    return {"template": TEMPLATE, "metrics": metrics, "dimensions": dimensions,
            "fields": {t: [k for k, v in spec["columns"].items() if v] for t, spec in tables.items()},
            "missing_inputs": {key: value for key, value in INPUT_HINTS.items() if key not in metrics + dimensions},
            "amount_mode": mode, "currency": "CNY", "status_map": mapping.get("status_map", {})}


def normalize_input(tables, settings):
    labels = {}
    for row in tables["order_items"]:
        for key, label in (("category_id", "category_name_zh"), ("seller_id", "seller_name")):
            if row.get(key) and row.get(label):
                previous = labels.setdefault((key, row[key]), row[label])
                if previous != row[label]:
                    raise ValueError(f"同一 {key} 对应多个名称，请核对映射")
    for row in tables["orders"]:
        status = row["order_status"]
        if str(status) not in settings["status_map"] and status not in {"delivered", "shipped", "approved", "processing", "created", "invoiced", "canceled", "cancelled", "unavailable"}:
            raise ValueError(f"请确认来源状态 {status!r} 的映射，已送达状态映射为 delivered")
        row["order_status"] = settings["status_map"].get(str(status), status)
    for row in tables["order_items"]:
        for key, label in (("category_id", "category_name_zh"), ("seller_id", "seller_name")):
            if row.get(key):
                row[label] = labels.get((key, row[key]), row[key])
        try:
            price = row.get("price")
            if settings["amount_mode"] == "unknown" or price in (None, ""):
                row["price"] = None
            elif settings["amount_mode"] == "unit":
                qty = row.get("quantity")
                row["price"] = None if qty in (None, "") else Decimal(str(price)) * Decimal(str(qty))
            for field in ("price", "freight_value"):
                if row.get(field) not in (None, ""):
                    amount = Decimal(str(row[field]))
                    if not amount.is_finite() or amount != amount.quantize(Decimal(".01")):
                        raise ValueError(f"{field} 需要精确到分的行金额")
        except InvalidOperation as error:
            raise ValueError(f"订单 {row['order_id']} 的金额或数量格式无效") from error


def validate_capabilities(plan, caps):
    if plan.intent in {"workflow", "clarify", "explain", "unsupported", "history", "load_catalog"}:
        return
    missing = set(plan.metric_ids) - set(caps["metrics"])
    missing.update(set(plan.group_by or [plan.dimension]) - set(caps["dimensions"]))
    allowed = {"order_id", "order_status", *caps["fields"]["order_items"]}
    for metric, field in (("sales", "order_sales"), ("freight", "order_freight"), ("payment_amount", "payment_amount"),
                          ("avg_review", "review_score")):
        if metric in caps["metrics"]:
            allowed.add(field)
    if "sales" not in caps["metrics"]:
        allowed.discard("price")
        if plan.intent == "diagnose" and "price_conflict" in plan.quality_types:
            missing.add("price")
    if "freight" not in caps["metrics"]:
        allowed.discard("freight_value")
    def used(node):
        if not node:
            return set()
        return {node.field} if node.field else set().union(*(used(child) for child in node.children))
    missing.update(({f.field for f in plan.filters} | used(plan.order_where) | used(plan.item_where)) - allowed)
    if plan.time_field != "purchase_at":
        missing.add(plan.time_field)
    if plan.intent == "details" and plan.detail_level in {"payments", "reviews"} and "order_"+plan.detail_level not in caps["fields"]:
        missing.add(plan.detail_level)
    if plan.intent == "join_example" and "order_payments" not in caps["fields"]:
        missing.add("order_payments")
    if plan.intent == "join_example" and "sales" not in caps["metrics"]:
        missing.add("sales")
    if plan.intent == "diagnose" and plan.diagnosis not in {"quality_summary", "quality_rows"}:
        missing.add(plan.diagnosis)
    if plan.intent == "diagnose" and "missing_payment" in plan.quality_types and "order_payments" not in caps["fields"]:
        missing.add("order_payments")
    if missing:
        raise ValueError("当前快照未提供该分析所需来源或口径：" + "、".join(sorted(missing)) +
                         "。可查指标：" + "、".join(caps["metrics"]) + "。" +
                         "。".join(dict.fromkeys(INPUT_HINTS[m] for m in sorted(missing) if m in INPUT_HINTS)))
