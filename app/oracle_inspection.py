"""Independent raw-record checks. No production SQL or quality classifier imports."""
import json
from collections import defaultdict
from decimal import Decimal

from app.oracle import source_sum
from app.source import SCHEMA


def inspect_source(oracle, plan):
    if plan.diagnosis == "order_audit":
        clean = plan.model_copy(update={"filters": [], "order_where": None, "item_where": None, "detail_level": "orders"})
        rows = oracle.details(clean)
        for row in rows:
            oid = row["order_id"]
            amounts = [i["price"] for i in oracle.items[oid] if i["seller_id"] == plan.audit_seller_id]
            row.update(selected_seller_id=plan.audit_seller_id, seller_sales=source_sum(amounts) if amounts else None,
                       payment_records=oracle.payment_rows[oid])
        return rows
    if plan.diagnosis == "order_candidate":
        selected = oracle.details(plan.model_copy(update={"detail_level": "items"}))
        eligible = [r for r in selected if r["membership_confirmed"] is True and
                    (not oracle.context.excludes_orders or r["order_id"] not in oracle.excluded_orders)]
        if not eligible:
            return []
        oid = min(r["order_id"] for r in eligible)
        return [{"entity_id": oid, "entity_name": oid, "order_id": oid,
                 "seller_id": min(r["seller_id"] for r in eligible if r["order_id"] == oid), "membership_confirmed": True}]
    periods = [plan.comparison_range] if plan.quality_scope == "previous" else [plan.time_range]
    if plan.quality_scope == "comparison_union" and plan.comparison_range:
        periods.append(plan.comparison_range)
    members = {}
    for period in periods:
        for row in oracle.details(plan.model_copy(update={"time_range": period, "detail_level": "orders"})):
            oid = row["order_id"]
            members[oid] = True if row["membership_confirmed"] is True or members.get(oid) is True else None
    variants = defaultdict(list)
    for raw in oracle.raw_tables["order_items"]:
        values = []
        for field, kind in SCHEMA["order_items"].items():
            value = raw[field]
            if value in (None, ""):
                value = None
            elif kind.startswith("DECIMAL"):
                value = Decimal(str(value))
            elif kind == "INTEGER":
                value = int(value)
            values.append(value)
        variants[(raw["order_id"], int(raw["order_item_id"]))].append(tuple(values))
    problems = []
    price_index = list(SCHEMA["order_items"]).index("price")
    for (oid, iid), records in variants.items():
        if oid not in members:
            continue
        unique = set(records)
        prices = {r[price_index] for r in unique if r[price_index] is not None}
        for kind, present in (("exact_duplicate", len(unique) < len(records)), ("price_conflict", len(prices) > 1)):
            if present and kind in plan.quality_types:
                problems.append({"issue_type": kind, "order_id": oid, "business_key": json.dumps([oid, iid]),
                    "membership_confirmed": members[oid], "extra_duplicate_rows": len(records)-len(unique) if kind == "exact_duplicate" else 0,
                    "candidate_count": len(unique)})
    if "missing_payment" in plan.quality_types:
        problems.extend({"issue_type": "missing_payment", "order_id": oid, "business_key": oid,
            "membership_confirmed": match, "extra_duplicate_rows": 0, "candidate_count": 0}
            for oid, match in members.items() if oid not in oracle.payments)
    if plan.diagnosis == "quality_rows":
        return sorted(problems, key=lambda r: (r["issue_type"], r["order_id"], r["business_key"]))
    summaries = []
    for kind in [*dict.fromkeys(plan.quality_types), "all"]:
        selected = [r for r in problems if kind == "all" or r["issue_type"] == kind]
        confirmed = [r for r in selected if r["membership_confirmed"] is True]
        summaries.append({"issue_type": kind, "issue_key_count": len({r["business_key"] for r in confirmed}),
            "affected_orders": len({r["order_id"] for r in confirmed}),
            "uncertain_orders": len({r["order_id"] for r in selected if r["membership_confirmed"] is None}),
            "extra_duplicate_rows": sum(r["extra_duplicate_rows"] for r in confirmed)})
    return summaries
