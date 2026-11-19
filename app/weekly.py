"""Weekly operating review calculations on an immutable, verified business snapshot."""

from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb

from app.inspection import quality_query
from app.models import AnalysisPlan, TimeRange
from app.semantic import validate_plan
from app.snapshot_diff import member_projection, read_analysis, rows
from app.snapshots import digest, source_path
from app.source import SCHEMA


def business_zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as err:
        raise ValueError("未知业务时区，请使用 Asia/Shanghai 等时区名称") from err


def last_week(as_of, zone, first_day):
    today = as_of or datetime.now(business_zone(zone)).date()
    end = today - timedelta(days=(today.weekday() - first_day) % 7)
    return TimeRange(start=end - timedelta(days=7), end=end)


def source_id(manifest):
    return manifest.get("delivery_source_id") or manifest["dataset_id"]


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def open_snapshot(executor):
    return duckdb.connect(str(executor.path), read_only=True, config={"enable_external_access": "false"})


def mapping_fingerprint(executor):
    for entry in executor.manifest["sources"]:
        path = source_path(entry, executor.path.parent)
        if path.name == "mapping.json":
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("留存的字段映射与快照不一致")
            mapping = json.loads(raw)
            return digest({t: v["columns"] for t, v in mapping["tables"].items()})
    return None


def order_projection(conn, scoped, ctx, statements, *, category):
    """Use the report's membership and amount predicates for per-order evidence."""
    sql, params = member_projection(scoped, "order_count", ctx)
    members = {
        r["order_id"]: {"confirmed": r["member"], "items": {}, "conflicts": {}}
        for r in rows(conn, sql, params, statements, ctx)
    }
    money = {"sales", "freight"} & set(scoped.metric_ids)
    if money:
        money.add("sales")
    item_plan = scoped.model_copy(update={"dimension": "category"}) if category else scoped
    for metric in sorted(money):
        sql, params = member_projection(item_plan, metric, ctx)
        for r in rows(conn, sql, params, statements, ctx):
            item = members[r["order_id"]]["items"].setdefault(str(r["order_item_id"]), {})
            item.update(eligible=r["amount_eligible"], confirmed=r["member"])
            item[metric] = r["value"]
            if category:
                item["category_id"] = r["entity_id"]
    if money and (scoped.item_where or any(f.field in {"seller_id", "category_id"} for f in scoped.filters)):
        membership_sql, params = member_projection(scoped, "order_count", ctx)
        sql = f"""SELECT i.order_id,i.order_item_id FROM order_items i
                  JOIN ({membership_sql}) m ON m.order_id=i.order_id"""
        for r in rows(conn, sql, params, statements, ctx):
            members[r["order_id"]].setdefault("source_keys", []).append(str(r["order_item_id"]))
    if money:
        diagnostic = scoped.model_copy(
            update={
                "intent": "diagnose",
                "diagnosis": "quality_rows",
                "quality_types": ["price_conflict"],
                "quality_scope": "current",
            }
        )
        compiled = quality_query(diagnostic, ctx)
        price = list(SCHEMA["order_items"]).index("price")
        for r in rows(conn, compiled.sql, compiled.parameters, statements, ctx):
            order = members[r["order_id"]]
            key = str(json.loads(r["business_key"])[1])
            order["conflicts"][key] = {
                "prices": sorted({str(v[price]) for v in json.loads(r["candidates"]) if v[price] is not None}),
                "source_rows": json.loads(r["source_rows"]),
            }
    return members


def calculate(executor, template, week):
    manifest = executor.manifest
    offset = timedelta(weeks=template["compare_weeks"])
    previous = TimeRange(start=week.start - offset, end=week.end - offset)
    base = AnalysisPlan.model_validate(template["plan"]).model_copy(
        update={
            "dataset_id": manifest["dataset_id"],
            "metadata_version": manifest["metadata_version"],
            "intent": "compare",
            "time_range": week,
            "comparison_range": previous,
            "period_mode": "explicit_range",
            "comparison_mode": "fixed",
        }
    )
    validate_plan(base, manifest)
    log = []
    with open_snapshot(executor) as conn:
        overview = read_analysis(conn, base, log, executor.context)
        if "current_value" in overview[0]:
            metric = base.metric_ids[0]
            cols = {
                "current_value": "current",
                "previous_value": "previous",
                "absolute_change": "change",
                "change_rate": "rate",
            }
            row = overview[0]
            for old, suffix in cols.items():
                row[f"{metric}_{suffix}"] = row.pop(old)
                if old in row["quality"]:
                    row["quality"][f"{metric}_{suffix}"] = row["quality"].pop(old)
        categories = []
        if template["category"]:
            category = base.model_copy(
                update={
                    "intent": "contribution",
                    "dimension": "category",
                    "metric_ids": ["sales"],
                    "limit_mode": "all",
                }
            )
            validate_plan(category, manifest)
            categories = read_analysis(conn, category, log, executor.context)
        orders = {}
        for name, period in (("previous", previous), ("current", week)):
            scoped = base.model_copy(
                update={"intent": "aggregate", "time_range": period, "comparison_range": None}
            )
            orders[name] = order_projection(conn, scoped, executor.context, log, category=template["category"])
    return {
        "plan": base.model_dump(mode="json"),
        "overview": overview,
        "categories": categories,
        "rule": executor.context.rule_version,
        "snapshot": manifest["database_sha256"],
        "metadata": manifest["metadata_sha256"],
        "mapping": mapping_fingerprint(executor),
        "order_evidence": orders,
        "statements": log,
    }


def restore_archived_categories(report, executor_for):
    """Complete older item evidence in memory from the report's original snapshot."""
    missing = {
        role: orders
        for role, orders in report["order_evidence"].items()
        if any("category_id" not in item for order in orders.values() for item in order["items"].values())
    }
    if not missing:
        return
    executor = executor_for(report["dataset"])
    if executor.manifest["database_sha256"] != report["snapshot"]:
        raise ValueError("原始快照与归档周报不一致，无法核对品类变更")
    base = AnalysisPlan.model_validate(report["plan"])
    with open_snapshot(executor) as conn:
        for role, orders in missing.items():
            period = base.time_range if role == "current" else base.comparison_range
            scoped = base.model_copy(update={
                "dimension": "category", "intent": "aggregate", "time_range": period, "comparison_range": None,
            })
            sql, params = member_projection(scoped, "sales", executor.context)
            for r in rows(conn, sql, params, report["statements"], executor.context):
                orders[r["order_id"]]["items"][str(r["order_item_id"])]["category_id"] = r["entity_id"]


def compare_reports(before, after, executor_for):
    keys = ("metric_ids", "time_field", "order_statuses", "filters", "item_where", "order_where")
    if (
        before["definition"] != after["definition"]
        or before["rule"] != after["rule"]
        or any(before["plan"][k] != after["plan"][k] for k in keys)
    ):
        different_object = any(
            before["plan"][k] != after["plan"][k] for k in ("filters", "order_where", "item_where")
        )
        changed = {k: [before["plan"][k], after["plan"][k]] for k in keys if before["plan"][k] != after["plan"][k]}
        for k in (
            "rule",
            "mapping",
            "metadata",
            "timezone",
            "first_day",
            "compare_weeks",
            "category",
            "net_refunds",
        ):
            if before[k] != after[k]:
                changed[k] = [before[k], after[k]]
        return {"kind": "分析对象变更" if different_object else "口径变更", "changed": changed}
    if after["category"]:
        for report in (before, after):
            restore_archived_categories(report, executor_for)
    changes = []
    delta = {}

    def business_values(order):
        if order is None:
            return None
        return {
            "confirmed": order["confirmed"],
            "items": order["items"],
            "conflicts": {key: issue["prices"] for key, issue in order["conflicts"].items()},
        }

    for period in ("previous", "current"):
        old, new = (r["order_evidence"][period] for r in (before, after))
        for oid in sorted(old.keys() | new.keys()):
            if business_values(old.get(oid)) != business_values(new.get(oid)):
                changes.append({"period": period, "order": oid, "before": old.get(oid), "after": new.get(oid)})
        for metric in after["plan"]["metric_ids"]:
            key = metric + "_" + period
            a, b = (r["overview"][0].get(key) for r in (before, after))
            delta[key] = Decimal(str(b)) - Decimal(str(a)) if a is not None and b is not None else None
    revision = {"kind": "同口径数据修订", "delta": delta, "changed_orders": changes}
    if "refunds" in after:
        revision["changed_refunds"] = []
        for period in ("previous", "current"):
            old, new = (r["refunds"][period] for r in (before, after))
            a, b = (r["currencies"]["CNY"]["net"] for r in (old, new))
            delta["net_cny_" + period] = Decimal(b) - Decimal(a) if a is not None and b is not None else None
            ledgers = [{r["id"]: r for r in v["postings"]} for v in (old, new)]
            for key in sorted(ledgers[0].keys() | ledgers[1].keys()):
                previous, current = (v.get(key) for v in ledgers)
                if previous != current:
                    revision["changed_refunds"].append(
                        {"period": period, "refund": key, "before": previous, "after": current}
                    )
    return revision
