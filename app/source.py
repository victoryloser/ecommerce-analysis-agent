"""来源表读取：导入器与独立基准共用 IO，不共用计算逻辑。"""
import csv
from pathlib import Path

from openpyxl import load_workbook

from app.config import ROOT

SCHEMA = {
    "customers": {"customer_id": "VARCHAR", "customer_unique_id": "VARCHAR", "customer_zip_code": "VARCHAR", "customer_city": "VARCHAR", "customer_state": "VARCHAR"},
    "geolocation": {"zip_code": "VARCHAR", "latitude": "DOUBLE", "longitude": "DOUBLE", "city": "VARCHAR", "state": "VARCHAR"},
    "orders": {"order_id": "VARCHAR", "customer_id": "VARCHAR", "order_status": "VARCHAR", "purchase_at": "TIMESTAMP", "approved_at": "TIMESTAMP", "shipped_at": "TIMESTAMP", "delivered_at": "TIMESTAMP", "estimated_delivery_at": "TIMESTAMP"},
    "order_items": {"order_id": "VARCHAR", "order_item_id": "INTEGER", "product_id": "VARCHAR", "seller_id": "VARCHAR", "shipping_limit_at": "TIMESTAMP", "price": "DECIMAL(18,2)", "freight_value": "DECIMAL(18,2)"},
    "order_payments": {"order_id": "VARCHAR", "payment_sequence": "INTEGER", "payment_type": "VARCHAR", "payment_installments": "INTEGER", "payment_value": "DECIMAL(18,2)"},
    "order_reviews": {"review_id": "VARCHAR", "order_id": "VARCHAR", "review_score": "INTEGER", "review_title": "VARCHAR", "review_message": "VARCHAR", "review_created_at": "TIMESTAMP", "seller_answered_at": "TIMESTAMP"},
    "products": {"product_id": "VARCHAR", "category_id": "VARCHAR", "product_name": "VARCHAR", "product_name_length": "INTEGER", "product_description_length": "INTEGER", "product_photos_count": "INTEGER", "product_weight_g": "INTEGER", "product_length_cm": "INTEGER", "product_height_cm": "INTEGER", "product_width_cm": "INTEGER", "base_price": "DECIMAL(18,2)"},
    "sellers": {"seller_id": "VARCHAR", "seller_name": "VARCHAR", "seller_zip_code": "VARCHAR", "seller_city": "VARCHAR", "seller_state": "VARCHAR"},
    "categories": {"category_id": "VARCHAR", "category_name_en": "VARCHAR", "category_name_zh": "VARCHAR"},
}
PRIMARY_KEYS = {
    "customers": ["customer_id"], "geolocation": ["zip_code"], "orders": ["order_id"],
    "order_items": ["order_id", "order_item_id"], "order_payments": ["order_id", "payment_sequence"],
    "order_reviews": ["review_id"], "products": ["product_id"], "sellers": ["seller_id"], "categories": ["category_id"],
}
FOREIGN_KEYS = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("order_items", "seller_id", "sellers", "seller_id"),
    ("order_payments", "order_id", "orders", "order_id"),
    ("order_reviews", "order_id", "orders", "order_id"),
    ("products", "category_id", "categories", "category_id"),
]


def source_files(dataset_id: str) -> list[Path]:
    if dataset_id == "excel_full":
        return [ROOT / "material/ecommerce_agent_demo.xlsx"]
    if dataset_id == "csv_small":
        return sorted((ROOT / "material/ecommerce_agent_demo_csv").glob("*.csv"))
    raise ValueError(f"未知数据集：{dataset_id}")


def read_source(dataset_id: str) -> dict[str, list[dict]]:
    if dataset_id == "csv_small":
        tables = {}
        for path in source_files(dataset_id):
            with path.open(encoding="utf-8-sig", newline="") as handle:
                tables[path.stem] = list(csv.DictReader(handle))
        return tables
    workbook = load_workbook(source_files(dataset_id)[0], read_only=True, data_only=True)
    tables = {}
    for sheet in workbook:
        rows = sheet.iter_rows(values_only=True)
        headers = tuple(next(rows))
        if sheet.title in SCHEMA:
            tables[sheet.title] = [dict(zip(headers, row)) for row in rows]
        elif headers and headers[0] == "question_id":
            tables["practice_questions"] = [dict(zip(headers, row)) for row in rows]
    workbook.close()
    return tables
