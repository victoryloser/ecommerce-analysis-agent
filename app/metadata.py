"""指标、字段、关系的业务定义；编译器和元数据接口共用同一口径。"""
from app.source import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMA

DIMENSIONS = ["total", "month", "category", "seller", "delivery_status", "customer_state"]
EXPRESSIONS = {
    "sales": "COALESCE(SUM(sales), 0)",
    "order_count": "COUNT(DISTINCT order_id)",
    "payment_amount": "COALESCE(SUM(payment_amount), 0)",
    "delay_rate": "CASE WHEN COUNT(*) FILTER (WHERE delivered_at IS NULL OR estimated_delivery_at IS NULL)>0 THEN NULL ELSE CAST(COUNT(*) FILTER (WHERE delivered_at>estimated_delivery_at) AS DOUBLE)/NULLIF(COUNT(*),0) END",
    "avg_review": "AVG(review_score)",
    "low_score_rate": "CAST(COUNT(*) FILTER (WHERE review_score<=2) AS DOUBLE)/NULLIF(COUNT(review_score),0)",
    "repeat_customers": "COUNT(*) FILTER (WHERE n>=2)",
}
METRIC_DETAILS = {
    "sales": {"aliases": ["商品销售额", "商品价款", "销售额"], "dependencies": ["order_items.price"], "grain": "订单—目标明细维度", "unit": "元", "denominator": None, "missing": "无记录为0；不含运费"},
    "order_count": {"aliases": ["订单数", "已送达订单数"], "dependencies": ["orders.order_id"], "grain": "分组内去重订单", "unit": "笔", "denominator": None, "missing": "无订单为0"},
    "delay_rate": {"aliases": ["延迟率", "晚到比例"], "dependencies": ["orders.order_id", "orders.delivered_at", "orders.estimated_delivery_at"], "grain": "订单—目标维度", "unit": "比例", "denominator": "分组内已送达订单数", "missing": "履约时间缺失时该组比例为NULL，并报告缺失数"},
    "avg_review": {"aliases": ["平均评分", "平均评价"], "dependencies": ["order_reviews.review_score"], "grain": "有评价的已送达订单", "unit": "分", "denominator": "有评价订单数", "missing": "未评价不记0；一单多评价必须先明确口径"},
    "low_score_rate": {"aliases": ["低分率", "差评率"], "dependencies": ["order_reviews.review_score"], "grain": "有评价的已送达订单", "unit": "比例", "denominator": "有评价订单数", "missing": "无评价为NULL；低分是评分≤2"},
    "repeat_customers": {"aliases": ["复购客户数", "回头客", "复购自然客户"], "dependencies": ["customers.customer_unique_id", "orders.order_id"], "grain": "统计期内自然客户", "unit": "人", "denominator": None, "missing": "无复购为0；至少2笔已送达订单"},
    "payment_amount": {"aliases": ["支付金额", "支付额"], "dependencies": ["order_payments.payment_value"], "grain": "先按订单汇总支付", "unit": "元", "denominator": None, "missing": "无支付记录为0；无商家/品类分摊规则"},
}
TABLE_GRAINS = {"orders": "订单", "customers": "客户记录，customer_unique_id才是自然客户", "order_items": "订单商品明细", "order_payments": "订单支付记录", "order_reviews": "评价记录", "products": "商品", "sellers": "商家", "categories": "品类", "geolocation": "邮编"}
FIELD_MEANINGS = {
    "order_id": "订单唯一标识/关联键", "customer_id": "客户记录标识", "customer_unique_id": "跨客户记录的自然客户标识",
    "order_status": "订单状态，本应用只统计delivered", "purchase_at": "下单时间，统计日期字段",
    "delivered_at": "实际送达时间", "estimated_delivery_at": "预计送达时间",
    "price": "商品明细价款，不含运费", "freight_value": "运费，不计商品销售额",
    "payment_value": "单条支付金额", "review_score": "评价分数，≤2为低分",
    "category_id": "品类标识", "seller_id": "商家标识", "product_id": "商品标识",
    "customer_zip_code": "客户邮编", "customer_city": "客户城市", "customer_state": "客户省份",
    "zip_code": "邮编", "latitude": "纬度", "longitude": "经度", "city": "城市", "state": "省份",
    "approved_at": "支付审批时间", "shipped_at": "发货时间", "order_item_id": "订单内明细序号",
    "shipping_limit_at": "最晚发货时间", "payment_sequence": "订单内支付序号", "payment_type": "支付方式",
    "payment_installments": "分期期数", "review_id": "评价标识", "review_title": "评价标题",
    "review_message": "评价正文", "review_created_at": "评价创建时间", "seller_answered_at": "商家回复时间",
    "product_name": "商品名称", "product_name_length": "商品名称长度", "product_description_length": "商品描述长度",
    "product_photos_count": "商品图片数量", "product_weight_g": "商品重量，克", "product_length_cm": "商品长度，厘米",
    "product_height_cm": "商品高度，厘米", "product_width_cm": "商品宽度，厘米", "base_price": "商品基础标价，不用于成交销售额",
    "seller_name": "商家名称", "seller_zip_code": "商家邮编", "seller_city": "商家城市", "seller_state": "商家省份",
    "category_name_en": "品类英文名称", "category_name_zh": "品类中文名称",
}


def metric_detail(metric_id: str) -> dict:
    dimensions = ["total", "month", "customer_state"] if metric_id == "repeat_customers" else [d for d in DIMENSIONS if d not in {"seller", "category"}] if metric_id == "payment_amount" else DIMENSIONS
    return {"id": metric_id, **METRIC_DETAILS[metric_id], "expression": EXPRESSIONS[metric_id],
            "required_filter": {"orders.order_status": "delivered"}, "time_field": "orders.purchase_at",
            "dimensions": dimensions, "rounding": "金额、评分和展示百分比保留2位，HALF_UP；中间金额DECIMAL"}


def table_detail(table: str) -> dict:
    return {"grain": TABLE_GRAINS[table], "primary_key": PRIMARY_KEYS[table],
            "fields": {name: {"type": kind, "meaning": FIELD_MEANINGS[name]} for name, kind in SCHEMA[table].items()},
            "relationships": [{"field": field, "target": f"{parent}.{key}", "cardinality": "N:1"}
                              for child, field, parent, key in FOREIGN_KEYS if child == table]}


def required_tables(metric_ids: list[str], dimension: str, filter_fields: list[str], intent: str) -> set[str]:
    if intent == "join_example":
        return {"orders", "order_items", "order_payments"}
    tables = {"orders", "customers"}
    if "sales" in metric_ids or dimension in {"seller", "category"} or set(filter_fields) & {"seller_id", "category_id"}:
        tables.update({"order_items", "products", "categories", "sellers"})
    if "payment_amount" in metric_ids:
        tables.add("order_payments")
    if set(metric_ids) & {"avg_review", "low_score_rate"}:
        tables.add("order_reviews")
    return tables
