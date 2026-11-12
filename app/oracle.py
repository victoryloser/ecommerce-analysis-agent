"""参数变体的独立计算基准：直接遍历原始来源，不依赖数据库或编译器。"""
from collections import Counter, defaultdict
from decimal import Decimal
from math import isclose

from app.models import AnalysisPlan, QueryResult
from app.source import read_source


class SourceOracle:
    def __init__(self, dataset_id: str):
        tables = read_source(dataset_id)
        self.orders = tables["orders"]
        self.customers = {r["customer_id"]: r for r in tables["customers"]}
        self.products = {r["product_id"]: r for r in tables["products"]}
        self.sellers = {r["seller_id"]: r["seller_name"] for r in tables["sellers"]}
        self.categories = {r["category_id"]: r["category_name_zh"] for r in tables["categories"]}
        self.items = defaultdict(list)
        self.payments = defaultdict(Decimal)
        self.payment_rows = Counter()
        self.reviews = {}
        for item in tables["order_items"]:
            self.items[item["order_id"]].append(item)
        for payment in tables["order_payments"]:
            self.payments[payment["order_id"]] += Decimal(str(payment["payment_value"]))
            self.payment_rows[payment["order_id"]] += 1
        for review in tables["order_reviews"]:
            if review["order_id"] in self.reviews:
                raise ValueError("独立基准需要明确的一单一评价口径")
            self.reviews[review["order_id"]] = Decimal(str(review["review_score"]))

    def evaluate(self, plan: AnalysisPlan) -> list[dict]:
        if plan.intent == "join_example":
            for order in sorted(self.orders, key=lambda r: r["order_id"]):
                oid = order["order_id"]
                if order["order_status"] != "delivered" or not str(plan.time_range.start) <= str(order["purchase_at"])[:10] < str(plan.time_range.end):
                    continue
                items, payments = len(self.items[oid]), self.payment_rows[oid]
                if items > 1 and payments > 1:
                    sales = sum((Decimal(str(i["price"])) for i in self.items[oid]), Decimal(0))
                    paid = self.payments[oid]
                    return [{"order_id": oid, "item_rows": items, "payment_rows": payments, "joined_rows": items*payments,
                             "sales": sales, "payment_amount": paid, "inflated_sales": sales*payments, "inflated_payment": paid*items}]
            return []
        if plan.intent == "compare":
            current = self.evaluate(plan.model_copy(update={"intent": "aggregate", "comparison_range": None}))[0][plan.metric_ids[0]]
            previous = self.evaluate(plan.model_copy(update={"intent": "aggregate", "time_range": plan.comparison_range, "comparison_range": None}))[0][plan.metric_ids[0]]
            change = current - previous if current is not None and previous is not None else None
            return [{"current_value": current, "previous_value": previous, "absolute_change": change,
                     "change_rate": change / previous if change is not None and previous else None}]
        groups = defaultdict(list)
        filters = {f.field: set(f.values) for f in plan.filters}
        for order in self.orders:
            oid = order["order_id"]
            day = str(order[plan.time_field])[:10]
            if order["order_status"] not in plan.order_statuses or not str(plan.time_range.start) <= day < str(plan.time_range.end):
                continue
            customer = self.customers[order["customer_id"]]
            if "customer_state" in filters and customer["customer_state"] not in filters["customer_state"]:
                continue
            items = [i for i in self.items[oid]
                     if ("seller_id" not in filters or i["seller_id"] in filters["seller_id"])
                     and ("category_id" not in filters or self.products[i["product_id"]]["category_id"] in filters["category_id"])]
            if set(filters) & {"seller_id", "category_id"} and not items:
                continue
            if not order["delivered_at"] or not order["estimated_delivery_at"]:
                delivery = "unknown"
            else:
                delivery = "delayed" if str(order["delivered_at"]) > str(order["estimated_delivery_at"]) else "on_time"
            entities = defaultdict(list)
            if plan.dimension in {"seller", "category"}:
                for item in items:
                    key = item["seller_id"] if plan.dimension == "seller" else self.products[item["product_id"]]["category_id"]
                    name = self.sellers[key] if plan.dimension == "seller" else self.categories[key]
                    entities[(key, name)].append(item)
            else:
                key, name = {
                    "total": ("all", "全站"), "month": (day[:7], day[:7]),
                    "customer_state": (customer["customer_state"], customer["customer_state"]),
                    "delivery_status": (delivery, {"delayed": "延迟", "on_time": "准时", "unknown": "时间缺失"}[delivery]),
                }[plan.dimension]
                entities[(key, name)] = items
            for entity, entity_items in entities.items():
                groups[entity].append({
                    "order_id": oid, "customer": customer["customer_unique_id"], "delivery": delivery,
                    "sales": sum((Decimal(str(i["price"])) for i in entity_items), Decimal(0)),
                    "payment": self.payments[oid], "review": self.reviews.get(oid),
                })
        if plan.dimension == "total" and not groups:
            groups[("all", "全站")] = []
        rows = []
        for (entity, name), records in sorted(groups.items()):
            count = len({r["order_id"] for r in records})
            if plan.dimension != "total" and count < plan.min_group_count:
                continue
            reviews = [r["review"] for r in records if r["review"] is not None]
            delayed = sum(r["delivery"] == "delayed" for r in records)
            missing = sum(r["delivery"] == "unknown" for r in records)
            values = {
                "sales": sum((r["sales"] for r in records), Decimal(0)), "order_count": count,
                "payment_amount": sum((r["payment"] for r in records), Decimal(0)),
                "delay_rate": Decimal(delayed)/count if count and not missing else None,
                "avg_review": sum(reviews)/len(reviews) if reviews else None,
                "low_score_rate": Decimal(sum(r <= 2 for r in reviews))/len(reviews) if reviews else None,
                "repeat_customers": sum(n >= 2 for n in Counter(r["customer"] for r in records).values()),
            }
            row = {} if plan.dimension == "total" else {"entity_id": entity, "entity_name": name}
            row.update({m: values[m] for m in plan.metric_ids})
            if "delay_rate" in plan.metric_ids:
                row.update(sample_orders=count, delayed_orders=delayed, missing_timing_orders=missing)
            if set(plan.metric_ids) & {"avg_review", "low_score_rate"}:
                row["reviewed_orders"] = len(reviews)
            rows.append(row)
        if plan.intent == "rank":
            metric = plan.metric_ids[0]
            rows.sort(key=lambda r: (r[metric] is None, -r[metric] if r[metric] is not None else 0, r["entity_id"]))
            rank = 0
            for index, row in enumerate(rows):
                if index == 0 or row[metric] != rows[index-1][metric]:
                    rank = index + 1
                row["rank_no"] = rank
            rows = [r for r in rows if r["rank_no"] <= plan.top_n]
        return rows


def check_rows(result: QueryResult, expected: list[dict]) -> None:
    assert result.status in {"ok", "empty"}, f"{result.status}: {result.error}"
    assert not result.truncated, "验收结果被截断"
    assert len(result.rows) == len(expected), f"期望{len(expected)}行，实际{len(result.rows)}行"
    for actual_values, wanted in zip(result.rows, expected):
        actual = dict(zip(result.columns, actual_values))
        assert set(actual) == set(wanted), f"列不一致：{set(actual)} / {set(wanted)}"
        for key, value in wanted.items():
            observed = actual[key]
            if isinstance(value, Decimal):
                assert observed is not None, f"{key}: 期望{value}，实际NULL"
                if key in {"sales", "payment_amount", "current_value", "previous_value", "absolute_change"} and value.as_tuple().exponent >= -2:
                    assert Decimal(str(observed)) == value, f"{key}: {observed} != {value}"
                else:
                    assert isclose(float(observed), float(value), rel_tol=1e-12, abs_tol=1e-12), f"{key}: {observed} != {value}"
            else:
                assert observed == value, f"{key}: {observed} != {value}"
