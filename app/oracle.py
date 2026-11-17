"""参数变体的独立计算基准：直接遍历原始来源，不依赖数据库或编译器。"""
from collections import Counter, defaultdict
from decimal import Decimal
from datetime import datetime
from math import isclose

from app.models import AnalysisPlan, QueryResult
from app.source import read_source
from app.config import DATA_DIR


def truth_all(values):
    return False if False in values else None if None in values else True


def truth_any(values):
    return True if True in values else None if None in values else False


def source_predicate(node, values):
    if node is None:
        return True
    if node.children:
        children = [source_predicate(child, values) for child in node.children]
        result = truth_all(children) if node.conjunction == "all" else truth_any(children)
    else:
        value, op, wanted = values[node.field], node.operator, node.values
        if isinstance(value, Decimal):
            wanted = [Decimal(str(item)) for item in wanted]
        if op == "is_null":
            result = value is None
        elif op == "not_null":
            result = value is not None
        elif value is None:
            result = False if node.field == "review_score" else None
        elif op in {"in", "not_in"}:
            result = (value in wanted) if op == "in" else (value not in wanted)
        elif op == "between":
            result = wanted[0] <= value <= wanted[1]
        else:
            other = wanted[0]
            result = {"eq": lambda: value == other, "ne": lambda: value != other,
                      "gt": lambda: value > other, "ge": lambda: value >= other,
                      "lt": lambda: value < other, "le": lambda: value <= other}[op]()
    return not result if node.negate and result is not None else result


def source_sum(values):
    return sum(values, Decimal(0)) if all(v is not None for v in values) else None


class SourceOracle:
    def __init__(self, dataset_id: str, tables=None, *, data_dir=DATA_DIR):
        if tables is None:
            from app.semantic import load_manifest
            from app.snapshots import read_retained
            tables = read_retained(load_manifest(dataset_id, data_dir), data_dir) if (data_dir / f"{dataset_id}.manifest.json").exists() else read_source(dataset_id, data_dir)
        self.raw_reviews = tables["order_reviews"]
        self.payment_details = defaultdict(list)
        self.quality_events = []
        self.orders = tables["orders"]
        self.customers = {r["customer_id"]: r for r in tables["customers"]}
        self.products = {r["product_id"]: r for r in tables["products"]}
        self.sellers = {r["seller_id"]: r["seller_name"] for r in tables["sellers"]}
        self.categories = {r["category_id"]: r["category_name_zh"] for r in tables["categories"]}
        self.items = defaultdict(list)
        self.payments = {}
        self.payment_rows = Counter()
        self.reviews = {}
        # 独立按业务键逐字段比较；不调用导入器或 SQL 的质量分类。
        for table, key, amounts in [("order_items", "order_item_id", ("price", "freight_value")),
                                     ("order_payments", "payment_sequence", ("payment_value",))]:
            records = {}
            variants = defaultdict(list)
            for raw in tables[table]:
                record = dict(raw)
                for field in amounts:
                    record[field] = Decimal(str(record[field])) if record[field] not in (None, "") else None
                identity = (record["order_id"], int(record[key]))
                variants[identity].append(record.copy())
                if identity in records:
                    for field in amounts:
                        if record[field] != records[identity][field]:
                            records[identity][field] = None
                else:
                    records[identity] = record
            for record in records.values():
                oid = record["order_id"]
                if table == "order_items":
                    self.items[oid].append(record)
                else:
                    self.payment_details[oid].append(record)
                    prior = self.payments.get(oid, Decimal(0))
                    self.payments[oid] = prior + record["payment_value"] if prior is not None and record["payment_value"] is not None else None
                    self.payment_rows[oid] += 1
            for (oid, _), candidates in variants.items():
                if len({tuple(r.items()) for r in candidates}) < len(candidates):
                    self.quality_events.append((oid, "exact_duplicate"))
                for field in amounts:
                    values = {r[field] for r in candidates}
                    if len(values) > 1:
                        self.quality_events.append((oid, "amount_conflict"))
                    elif None in values:
                        self.quality_events.append((oid, "missing_value"))
        self.quality_events.extend((order["order_id"], "missing_record") for order in self.orders if order["order_id"] not in self.payments)
        for review in tables["order_reviews"]:
            if review["order_id"] in self.reviews:
                raise ValueError("独立基准需要明确的一单一评价口径")
            self.reviews[review["order_id"]] = Decimal(str(review["review_score"]))

    def details(self, plan):
        filters = {f.field: set(f.values) for f in plan.filters}
        rows = []
        def timestamp(value):
            return datetime.fromisoformat(str(value)) if value not in (None, "") else None
        for order in sorted(self.orders, key=lambda r: r["order_id"]):
            oid = order["order_id"]
            customer = self.customers[order["customer_id"]]
            if order["order_status"] not in plan.order_statuses or not str(plan.time_range.start) <= str(order[plan.time_field])[:10] < str(plan.time_range.end):
                continue
            if plan.selected_order_id and oid != plan.selected_order_id:
                continue
            if "customer_state" in filters and customer["customer_state"] not in filters["customer_state"]:
                continue
            amounts = {field: [i[field] for i in self.items[oid]] for field in ("price", "freight_value")}
            exact = {field: source_sum(values) if values else None for field, values in amounts.items()}
            payment_values = [r["payment_value"] for r in self.payment_details[oid]]
            delivery = "unknown" if not order["delivered_at"] or not order["estimated_delivery_at"] else "delayed" if str(order["delivered_at"]) > str(order["estimated_delivery_at"]) else "on_time"
            match = source_predicate(plan.order_where, {**order, "customer_state": customer["customer_state"], "order_sales": exact["price"],
                "order_freight": exact["freight_value"], "payment_amount": self.payments.get(oid), "review_score": self.reviews.get(oid), "delivery_status": delivery})
            items = []
            for item in self.items[oid]:
                category = self.products[item["product_id"]]["category_id"]
                if ("category_id" in filters and category not in filters["category_id"]) or ("seller_id" in filters and item["seller_id"] not in filters["seller_id"]):
                    continue
                state = source_predicate(plan.item_where, {**item, "category_id": category})
                if state is not False:
                    items.append((item, category, state))
            if plan.item_where or set(filters) & {"seller_id", "category_id"}:
                match = truth_all([match, truth_any([state for _, _, state in items])])
            if match is False:
                continue
            if plan.detail_level == "orders":
                row = {"entity_id": oid, "entity_name": oid, "order_id": oid, "order_status": order["order_status"],
                       **{field: timestamp(order[field]) for field in ("purchase_at", "delivered_at", "approved_at", "shipped_at", "estimated_delivery_at")},
                       "customer_unique_id": customer["customer_unique_id"], "customer_state": customer["customer_state"], "membership_confirmed": match,
                       "payment_amount": self.payments.get(oid), "payment_known": sum((v for v in payment_values if v is not None), Decimal(0)) if any(v is not None for v in payment_values) else None,
                       "payment_unknown": sum(v is None for v in payment_values), "payment_missing": not payment_values, "review_score": self.reviews.get(oid)}
                for field, name in (("price", "order_sales"), ("freight_value", "order_freight")):
                    values = amounts[field]
                    row.update({name: exact[field], name+"_known": sum((v for v in values if v is not None), Decimal(0)) if any(v is not None for v in values) else None,
                                name+"_unknown": sum(v is None for v in values)})
                rows.append(row)
            elif plan.detail_level == "items":
                for item, category, state in sorted(items, key=lambda entry: int(entry[0]["order_item_id"])):
                    rows.append({"entity_id": f"{oid}:{item['order_item_id']}", "entity_name": item["product_id"],
                        **{field: item[field] for field in ("order_id", "product_id", "seller_id", "price", "freight_value")},
                        "order_item_id": int(item["order_item_id"]), "product_name": self.products[item["product_id"]]["product_name"],
                        "category_id": category, "category_name_zh": self.categories[category], "seller_name": self.sellers[item["seller_id"]],
                        "membership_confirmed": truth_all([match, state])})
            elif plan.detail_level == "payments":
                for payment in sorted(self.payment_details[oid], key=lambda r: int(r["payment_sequence"])):
                    rows.append({"entity_id": f"{oid}:{payment['payment_sequence']}", "entity_name": payment["payment_type"], "order_id": oid,
                        "payment_sequence": int(payment["payment_sequence"]), "payment_type": payment["payment_type"],
                        "payment_installments": int(payment["payment_installments"]) if payment["payment_installments"] not in (None, "") else None,
                        "payment_value": payment["payment_value"], "membership_confirmed": match})
            else:
                for review in sorted((r for r in self.raw_reviews if r["order_id"] == oid), key=lambda r: r["review_id"]):
                    rows.append({"entity_id": review["review_id"], "entity_name": review["review_id"], "order_id": oid,
                        "review_score": int(review["review_score"]), "review_title": review["review_title"] or None, "review_message": review["review_message"] or None,
                        "review_created_at": timestamp(review["review_created_at"]), "membership_confirmed": match})
        return rows

    def evaluate(self, plan: AnalysisPlan) -> list[dict]:
        if plan.intent == "details":
            return self.details(plan)
        if plan.intent == "diagnose" and plan.diagnosis in {"reconciliation", "quality"}:
            selected = self.details(plan.model_copy(update={"detail_level": "orders"}))
            if plan.diagnosis == "reconciliation":
                rows = []
                threshold = Decimal(str(plan.diagnostic_threshold if plan.diagnostic_threshold is not None else .01))
                for record in selected:
                    complete = all(record[key] is not None for key in ("payment_amount", "order_sales", "order_freight"))
                    delta = record["payment_amount"]-record["order_sales"]-record["order_freight"] if complete else None
                    if delta is None or abs(delta) > threshold:
                        rows.append({**{key: record[key] for key in ("entity_id", "entity_name", "order_id", "order_sales", "order_freight", "payment_amount")}, "difference": delta})
                return sorted(rows, key=lambda r: (r["difference"] is not None, -abs(r["difference"] or 0), r["entity_id"]))
            ids = {r["order_id"] for r in selected}
            pairs = [("delivered_at", "purchase_at"), ("approved_at", "purchase_at"), ("shipped_at", "approved_at"), ("delivered_at", "shipped_at"), ("estimated_delivery_at", "purchase_at")]
            counts = {"missing_timing": sum(r["order_status"] == "delivered" and (r["delivered_at"] is None or r["estimated_delivery_at"] is None) for r in selected),
                      "duplicate_reviews": 0, "negative_amount": sum((item["price"] is not None and item["price"] < 0) or (item["freight_value"] is not None and item["freight_value"] < 0) for oid in ids for item in self.items[oid]),
                      "invalid_date": sum(any(r[a] and r[b] and r[a] < r[b] for a, b in pairs) for r in selected)}
            counts.update(Counter(reason for oid, reason in self.quality_events if oid in ids))
            labels = {"missing_timing": "履约时间缺失", "duplicate_reviews": "一单多评价", "negative_amount": "负商品或运费", "invalid_date": "日期先后异常"}
            return [{"entity_id": key, "entity_name": labels.get(key, key), "issue_count": value} for key, value in sorted(counts.items())]
        if plan.intent == "diagnose" and plan.diagnosis == "sales_spike":
            monthly = self.evaluate(plan.model_copy(update={"intent": "aggregate", "dimension": "month", "metric_ids": ["sales"], "min_group_count": 1}))
            rows = []
            threshold = Decimal(str(plan.diagnostic_threshold if plan.diagnostic_threshold is not None else .3))
            for prior, current in zip(monthly, monthly[1:]):
                py, pm = map(int, prior["entity_id"].split("-")); cy, cm = map(int, current["entity_id"].split("-"))
                if (cy-py)*12+cm-pm != 1:
                    continue
                rate = (current["sales"]-prior["sales"])/prior["sales"] if current["sales"] is not None and prior["sales"] else None
                if rate is not None and abs(rate) >= threshold:
                    rows.append({**current, "previous_sales": prior["sales"], "previous_month": prior["entity_id"], "change_rate": rate})
            return rows
        if plan.intent == "diagnose" and plan.diagnosis == "seller_outlier":
            import statistics
            sellers = self.evaluate(plan.model_copy(update={"intent": "aggregate", "dimension": "seller", "metric_ids": ["delay_rate", "order_count"]}))
            rates = [float(row["delay_rate"]) for row in sellers if row["delay_rate"] is not None]
            if not rates:
                return []
            mean, std = statistics.mean(rates), statistics.pstdev(rates)
            threshold = plan.diagnostic_threshold if plan.diagnostic_threshold is not None else 2
            rows = []
            for row in sellers:
                z = (float(row["delay_rate"])-mean)/std if std and row["delay_rate"] is not None else None
                if z is not None and z >= threshold:
                    rows.append({**row, "mean_rate": Decimal(str(mean)), "std_rate": Decimal(str(std)), "z_score": Decimal(str(z))})
            return sorted(rows, key=lambda r: (-r["z_score"], r["entity_id"]))
        if plan.intent == "join_example":
            for order in sorted(self.orders, key=lambda r: r["order_id"]):
                oid = order["order_id"]
                if order["order_status"] != "delivered" or not str(plan.time_range.start) <= str(order["purchase_at"])[:10] < str(plan.time_range.end):
                    continue
                items, payments = len(self.items[oid]), self.payment_rows[oid]
                if items > 1 and payments > 1:
                    sales = source_sum([i["price"] for i in self.items[oid]])
                    paid = self.payments[oid]
                    return [{"order_id": oid, "item_rows": items, "payment_rows": payments, "joined_rows": items*payments,
                             "sales": sales, "payment_amount": paid, "inflated_sales": sales*payments if sales is not None else None, "inflated_payment": paid*items if paid is not None else None}]
            return []
        if plan.intent in {"compare", "contribution"} and (plan.dimension != "total" or len(plan.metric_ids) != 1 or plan.order_where or plan.item_where or plan.order_statuses != ["delivered"] or plan.time_field != "purchase_at"):
            common = {"intent": "aggregate", "comparison_range": None, "min_group_count": 1}
            metrics = list(dict.fromkeys([*plan.metric_ids, *(["order_count"] if plan.min_group_count > 1 else [])]))
            current = {r.get("entity_id", "all"): r for r in self.evaluate(plan.model_copy(update={**common, "metric_ids": metrics}))}
            previous = {r.get("entity_id", "all"): r for r in self.evaluate(plan.model_copy(update={**common, "metric_ids": metrics, "time_range": plan.comparison_range}))}
            rows = []
            for key in sorted(current.keys() | previous.keys()):
                c, p = current.get(key), previous.get(key)
                row = {"entity_id": key, "entity_name": (c or p)["entity_name"]} if plan.dimension != "total" else {}
                for metric in plan.metric_ids:
                    absent = 0 if metric in {"sales", "order_count", "payment_amount", "repeat_customers"} else None
                    cv, pv = c[metric] if c else absent, p[metric] if p else absent
                    delta = cv-pv if cv is not None and pv is not None else None
                    row.update({metric+"_current": cv, metric+"_previous": pv, metric+"_change": delta,
                                metric+"_rate": delta/pv if delta is not None and pv else None})
                if plan.min_group_count > 1:
                    row.update(current_orders=c["order_count"] if c else 0, previous_orders=p["order_count"] if p else 0)
                rows.append(row)
            if plan.intent == "contribution":
                metric = plan.metric_ids[0]
                total = source_sum([r[metric+"_change"] for r in rows])
                for row in rows:
                    row.update(total_change=total, contribution_rate=row[metric+"_change"]/total if total and row[metric+"_change"] is not None else None)
                rows.sort(key=lambda r: (r[metric+"_change"] is None, -(r[metric+"_change"] or 0), r["entity_id"]))
            return [r for r in rows if plan.min_group_count == 1 or
                    all(r[key] is None or r[key] >= plan.min_group_count for key in ("current_orders", "previous_orders"))]
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
            if plan.selected_order_id and oid != plan.selected_order_id:
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
            order_match = source_predicate(plan.order_where, {**order, "customer_state": customer["customer_state"],
                "order_sales": source_sum([i["price"] for i in self.items[oid]]) if self.items[oid] else None,
                "order_freight": source_sum([i["freight_value"] for i in self.items[oid]]) if self.items[oid] else None,
                "payment_amount": self.payments.get(oid), "review_score": self.reviews.get(oid), "delivery_status": delivery})
            items = [{**item, "match": source_predicate(plan.item_where, {**item, "category_id": self.products[item["product_id"]]["category_id"]})} for item in items]
            items = [item for item in items if item["match"] is not False]
            if plan.item_where is not None or set(filters) & {"seller_id", "category_id"}:
                order_match = truth_all([order_match, truth_any([i["match"] for i in items])])
            if order_match is False:
                continue
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
                match = truth_all([order_match, truth_any([i["match"] for i in entity_items])]) if plan.dimension in {"category", "seller"} else order_match
                amount = source_sum([i["price"] for i in entity_items]) if all(i["match"] is True for i in entity_items) else None
                groups[entity].append({
                    "match": match,
                    "order_id": oid, "customer": customer["customer_unique_id"], "delivery": delivery,
                    "sales": amount,
                    "payment": self.payments.get(oid), "review": self.reviews.get(oid),
                })
        if plan.dimension == "total" and not groups:
            groups[("all", "全站")] = []
        rows = []
        rank_uncertain = False
        for (entity, name), records in sorted(groups.items()):
            uncertain = [r for r in records if r["match"] is None]
            records = [r for r in records if r["match"] is True]
            count = len({r["order_id"] for r in records})
            if plan.dimension != "total" and count < plan.min_group_count and not uncertain:
                continue
            reviews = [r["review"] for r in records if r["review"] is not None]
            delayed = sum(r["delivery"] == "delayed" for r in records)
            missing = sum(r["delivery"] == "unknown" for r in records)
            values = {
                "sales": sum((r["sales"] for r in records), Decimal(0)) if all(r["sales"] is not None for r in records) else None, "order_count": count,
                "payment_amount": sum((r["payment"] for r in records), Decimal(0)) if all(r["payment"] is not None for r in records) else None,
                "delay_rate": Decimal(delayed)/count if count and not missing else None,
                "avg_review": sum(reviews)/len(reviews) if reviews else None,
                "low_score_rate": Decimal(sum(r <= 2 for r in reviews))/len(reviews) if reviews else None,
                "repeat_customers": sum(n >= 2 for n in Counter(r["customer"] for r in records).values()),
            }
            if uncertain:
                for metric in values:
                    values[metric] = None
                confirmed_customers = Counter(r["customer"] for r in records)
                uncertain_customers = Counter(r["customer"] for r in uncertain)
                unresolved = any(confirmed_customers[c] < 2 <= confirmed_customers[c]+n for c, n in uncertain_customers.items())
                values["repeat_customers"] = None if unresolved else sum(n >= 2 for n in confirmed_customers.values())
            if plan.intent == "rank":
                metric = plan.metric_ids[0]
                rank_uncertain |= values[metric] is None and (bool(uncertain) or metric in {"sales", "payment_amount", "repeat_customers"} or (metric == "delay_rate" and missing > 0))
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
            if rank_uncertain:
                for row in rows:
                    row["rank_no"] = None
                return rows
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
