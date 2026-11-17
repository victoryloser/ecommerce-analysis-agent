"""直接读取原始材料，以 Decimal 和集合独立计算；不依赖 SQL 或模型计划。"""
import argparse
import json
from collections import Counter, defaultdict
from decimal import Decimal

from app.config import DATA_DIR, DATASETS
from app.source import read_source


def calculate(dataset_id: str) -> dict:
    from app.snapshots import resolve_dataset
    from app.semantic import load_manifest
    dataset_id = resolve_dataset(dataset_id)
    tables = read_source(dataset_id)
    delivered = {r["order_id"]: r for r in tables["orders"] if r["order_status"] == "delivered"}
    customers = {r["customer_id"]: r["customer_unique_id"] for r in tables["customers"]}
    products = {r["product_id"]: r["category_id"] for r in tables["products"]}
    categories = {r["category_id"]: r["category_name_zh"] for r in tables["categories"]}
    sellers = {r["seller_id"]: r["seller_name"] for r in tables["sellers"]}
    sales = Decimal(0)
    monthly_sales = defaultdict(Decimal)
    category_sales = defaultdict(Decimal)
    seller_orders = defaultdict(set)
    all_order_sales = defaultdict(Decimal)
    for item in tables["order_items"]:
        oid = item["order_id"]
        if oid not in delivered:
            continue
        amount = Decimal(str(item["price"]))
        month = str(delivered[oid]["purchase_at"])[:7]
        sales += amount
        all_order_sales[oid] += amount
        monthly_sales[month] += amount
        if month == "2025-04":
            category_sales[products[item["product_id"]]] += amount
        if month == "2025-05":
            seller_orders[item["seller_id"]].add(oid)
    def is_late(order):
        return str(order["delivered_at"]) > str(order["estimated_delivery_at"])

    threshold = load_manifest(dataset_id)["practice"]["min_seller_orders"] if (DATA_DIR / f"{dataset_id}.manifest.json").exists() else (10 if dataset_id == "excel_full" else 3)
    delay_rates = {}
    for seller, orders in seller_orders.items():
        if len(orders) >= threshold:
            late = sum(is_late(delivered[oid]) for oid in orders)
            delay_rates[seller] = {"rate": Decimal(late) / Decimal(len(orders)), "orders": len(orders), "late": late}
    reviews = defaultdict(list)
    reviewed_orders = set()
    for review in tables["order_reviews"]:
        oid = review["order_id"]
        if oid not in delivered:
            continue
        if oid in reviewed_orders:
            raise ValueError("一单多评价，基准口径尚未定义")
        reviewed_orders.add(oid)
        group = "delayed" if is_late(delivered[oid]) else "on_time"
        reviews[group].append(Decimal(str(review["review_score"])))
    customer_counts = Counter(customers[r["customer_id"]] for r in delivered.values())
    winning_sales = max(category_sales.values())
    winning_rate = max(v["rate"] for v in delay_rates.values())
    payments = sum((Decimal(str(r["payment_value"])) for r in tables["order_payments"] if r["order_id"] in delivered), Decimal(0))
    return {
        "Q01": len(delivered), "Q02": sales,
        "Q03": {"current": monthly_sales["2025-04"], "previous": monthly_sales["2025-03"], "rate": (monthly_sales["2025-04"] - monthly_sales["2025-03"]) / monthly_sales["2025-03"]},
        "Q04": [{"id": cid, "name": categories[cid], "sales": amount} for cid, amount in sorted(category_sales.items()) if amount == winning_sales],
        "Q05": [{"id": sid, "name": sellers[sid], **values} for sid, values in sorted(delay_rates.items()) if values["rate"] == winning_rate],
        "Q06": {group: {"avg": sum(scores) / len(scores), "count": len(scores), "low_score_rate": Decimal(sum(s <= 2 for s in scores))/len(scores)} for group, scores in reviews.items()},
        "Q07": sum(n >= 2 for n in customer_counts.values()),
        "payment_amount": payments, "monthly_sales": dict(sorted(monthly_sales.items())),
        "monthly_orders": dict(sorted(Counter(str(o["purchase_at"])[:7] for o in delivered.values()).items())),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=[*DATASETS, "all"], default="all")
    args = parser.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    for dataset in DATASETS if args.dataset == "all" else [args.dataset]:
        result = calculate(dataset)
        (DATA_DIR / f"{dataset}.baseline.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(dataset, json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
