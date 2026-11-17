"""宿主限定的双快照只读比较；差异和勾稽使用完整结果。"""
import csv
import json
import multiprocessing as mp
import time
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path

import duckdb

from app.models import AnalysisPlan
from app.quality import QUERY_SCHEMA
from app.snapshots import canonical, quality_signature
from app.source import SCHEMA, PRIMARY_KEYS
from app.sql_compiler import compile_query
from app.sql_audit import audit_query
from app.sql_guard import validate_sql
from app.grouping import dimensions, entity_sql, result_columns

DIFF_VERSION = "snapshot-diff-v1"
ADDITIVE = {"sales", "payment_amount", "order_count", "repeat_customers"}
ORDER_PROJECTION = """WITH i AS (
    SELECT order_id,COUNT(*) AS item_count,SUM(price) AS sales_known,
        CASE WHEN COUNT(*)=COUNT(price) THEN SUM(price) END AS sales,
        CASE WHEN COUNT(*)=COUNT(freight_value) THEN SUM(freight_value) END AS freight
    FROM order_items GROUP BY order_id), p AS (
    SELECT order_id,COUNT(*) AS payment_count,SUM(payment_value) AS payment_known,
        CASE WHEN COUNT(*)=COUNT(payment_value) THEN SUM(payment_value) END AS payment_amount
    FROM order_payments GROUP BY order_id)
    SELECT o.order_id,o.purchase_at,o.order_status,
        CASE WHEN i.order_id IS NULL THEN 0 ELSE i.sales END AS sales,
        CASE WHEN i.order_id IS NULL THEN 0 ELSE i.freight END AS freight,
        i.sales_known,p.payment_amount,p.payment_known,(p.order_id IS NULL) AS payment_missing
    FROM orders o LEFT JOIN i ON i.order_id=o.order_id LEFT JOIN p ON p.order_id=o.order_id ORDER BY o.order_id"""


def rows(conn, sql, parameters, statements):
    validate_sql(sql, schema=QUERY_SCHEMA, exploration=True)
    statements.append({"sql": sql, "parameters": parameters})
    cursor = conn.cursor().execute(sql, parameters)
    columns = [c[0] for c in cursor.description]
    try:
        while batch := cursor.fetchmany(1000):
            for row in batch:
                yield dict(zip(columns, row))
    finally:
        cursor.close()


def aligned(left, right, key):
    a, b = next(left, None), next(right, None)
    while a is not None or b is not None:
        if b is None or (a is not None and key(a) < key(b)):
            yield key(a), a, None
            a = next(left, None)
        elif a is None or key(b) < key(a):
            yield key(b), None, b
            b = next(right, None)
        else:
            yield key(a), a, b
            a, b = next(left, None), next(right, None)


def change_kind(a, b):
    return "added" if a is None else "removed" if b is None else "changed"


def numeric_delta(a, b):
    return b-a if a is not None and b is not None else None


def read_analysis(conn, plan, statements):
    compiled = compile_query(plan)
    audit_query(plan, compiled)
    result = []
    for row in rows(conn, compiled.sql, compiled.parameters, statements):
        raw = row.pop("__quality", "{}")
        row["quality"] = json.loads(raw, parse_float=Decimal)
        result.append(row)
    return result


def value_state(row, metric):
    if row is None:
        return {"state": "absent", "value": Decimal(0) if metric in ADDITIVE else None, "quality": {}}
    q = row["quality"][metric]
    value = row[metric]
    state = "unknown" if q["completeness"] != "complete" else "not_applicable" if value is None else "known"
    return {"state": state, "value": value, "quality": q}


def scope_contract(plan):
    return {"group_by": list(dimensions(plan)), **{name: plan.model_dump(mode="json")[name] for name in
            ("time_field", "time_range", "comparison_range", "order_statuses", "filters",
             "order_where", "item_where", "selected_order_id", "metric_ids", "min_group_count")}}


def member_projection(plan, metric):
    from app.facts import scope_ctes
    ctes, parameters = scope_ctes(plan)
    entity, _ = entity_sql(plan)
    if metric == "sales":
        sql = f"""SELECT i.order_id,i.order_item_id,{entity} AS entity_id,i.price AS value,
            (o.scope_match AND i.item_match) AS member
            FROM items i JOIN scoped_orders o ON o.order_id=i.order_id"""
    else:
        # 订单在一个商品分组内只计一次；组内明细的存在条件保留 UNKNOWN。
        if set(dimensions(plan)) & {"category", "seller"}:
            ctes.append(f"""groups AS (SELECT i.order_id,{entity} AS entity_id,
                CASE WHEN COUNT(*) FILTER (WHERE item_match)>0 THEN TRUE
                     WHEN COUNT(*) FILTER (WHERE item_match IS NULL)>0 THEN NULL ELSE FALSE END AS member
                FROM items i JOIN scoped_orders o ON o.order_id=i.order_id GROUP BY i.order_id,entity_id)""")
            entity, join, member = "g.entity_id", "JOIN groups g ON g.order_id=o.order_id", "o.scope_match AND g.member"
        else:
            join, member = "", "o.scope_match"
        value = "1" if metric == "order_count" else "(SELECT CASE WHEN COUNT(*)=COUNT(p.payment_value) THEN SUM(p.payment_value) END FROM order_payments p WHERE p.order_id=o.order_id)"
        sql = f"SELECT o.order_id,0 AS order_item_id,{entity} AS entity_id,{value} AS value,({member}) AS member FROM scoped_orders o {join}"
    return "WITH " + ",".join(ctes) + " " + sql, parameters


def review_samples(conn, plan, statements):
    from app.facts import scope_ctes
    ctes, parameters = scope_ctes(plan)
    entity, _ = entity_sql(plan)
    if set(dimensions(plan)) & {"category", "seller"}:
        ctes.append(f"groups AS (SELECT i.order_id,{entity} AS entity_id,CASE WHEN COUNT(*) FILTER (WHERE item_match)>0 THEN TRUE WHEN COUNT(*) FILTER (WHERE item_match IS NULL)>0 THEN NULL ELSE FALSE END AS member FROM items i JOIN scoped_orders o ON o.order_id=i.order_id GROUP BY i.order_id,entity_id)")
        entity, join, member = "g.entity_id", "JOIN groups g ON g.order_id=o.order_id", "o.scope_match AND g.member"
    else:
        join, member = "", "o.scope_match"
    sql = "WITH " + ",".join(ctes) + f""" SELECT {entity} AS entity_id,
        COUNT(r.review_score) FILTER (WHERE {member}) AS reviewed_orders,
        SUM(r.review_score) FILTER (WHERE {member}) AS score_sum,
        COUNT(*) FILTER (WHERE ({member}) AND r.review_score<=2) AS low_score_orders
        FROM scoped_orders o {join} LEFT JOIN order_reviews r ON r.order_id=o.order_id GROUP BY entity_id"""
    return {r["entity_id"]: r for r in rows(conn, sql, parameters, statements)}


def compare_members(conns, plans, metric, statements, emit, period, order_sets):
    members = []
    for conn, plan, log in zip(conns, plans, statements):
        sql, params = member_projection(plan, metric)
        members.append({canonical([r["order_id"], r["order_item_id"], r["entity_id"]]): r for r in rows(conn, sql, params, log)})
    summaries = {}
    for key in sorted(members[0].keys() | members[1].keys()):
        a, b = members[0].get(key), members[1].get(key)
        entity = (a or b)["entity_id"]
        summary = summaries.setdefault(entity, {"common_change": Decimal(0), "entered_value": Decimal(0), "exited_value": Decimal(0),
            "common_count": 0, "entered_count": 0, "exited_count": 0, "unknown_count": 0})
        if (a and (a["member"] is None or a["value"] is None)) or (b and (b["member"] is None or b["value"] is None)):
            category, amount = "unconfirmed", None
            summary["unknown_count"] += 1
        elif a and b:
            category, amount = "common", b["value"]-a["value"]
            summary["common_change"] += amount
            summary["common_count"] += 1
        elif b:
            category, amount = "entered", b["value"]
            summary["entered_value"] += amount
            summary["entered_count"] += 1
        else:
            category, amount = "exited", a["value"]
            summary["exited_value"] += amount
            summary["exited_count"] += 1
        if category != "common" or amount:
            oid = (a or b)["order_id"]
            source_presence = "existing_order" if oid in order_sets[0] and oid in order_sets[1] else "source_added" if oid in order_sets[1] else "source_missing"
            emit("membership", [period, metric, entity, oid, (a or b)["order_item_id"]], category,
                 a, b, {"amount": amount, "source_presence": source_presence})
    for result in summaries.values():
        result["delta"] = None if result["unknown_count"] else result["common_change"] + result["entered_value"] - result["exited_value"]
    return summaries


def compare_snapshots(paths, plans, directory, comparable=True):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {"algorithm_version": DIFF_VERSION, "status": "completed", "structural_diff": {}, "business_diff": {},
              "quality_diff": {"changed": 0, "preview": []}, "metric_comparisons": [], "reconciliation": [], "preview": [], "diff_rows": 0}
    statements = [[], []]
    order_sets = [set(), set()]
    revised_orders, revised_items = set(), set()
    with ExitStack() as stack:
        conns = [stack.enter_context(duckdb.connect(str(path), read_only=True, config={
            "enable_external_access": "false", "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false", "memory_limit": "256MB", "threads": "2"})) for path in paths]
        handle = stack.enter_context((directory / "differences.csv").open("w", encoding="utf-8-sig", newline=""))
        writer = csv.writer(handle)
        writer.writerow(["layer", "key", "change", "before", "after", "explanation"])
        def emit(layer, key, kind, a, b, extra=None):
            record = {"layer": layer, "key": key, "change": kind, "before": a, "after": b, "explanation": extra}
            writer.writerow([layer, canonical(key), kind, canonical(a), canonical(b), canonical(extra)])
            report["diff_rows"] += 1
            if len(report["preview"]) < 500:
                report["preview"].append(record)
        for table in SCHEMA:
            keys = PRIMARY_KEYS[table]
            sql = f"SELECT * FROM {table} ORDER BY {','.join(keys)}"
            summary = {"added": 0, "removed": 0, "changed": 0, "unchanged": 0, "fields": {}}
            sources = [rows(conn, sql, [], log) for conn, log in zip(conns, statements)]
            for key, a, b in aligned(*sources, key=lambda r: tuple(r[k] for k in keys)):
                if table == "orders":
                    for index, record in enumerate((a, b)):
                        if record:
                            order_sets[index].add(record["order_id"])
                if a == b:
                    summary["unchanged"] += 1
                    continue
                kind = change_kind(a, b)
                summary[kind] += 1
                changed = [field for field in SCHEMA[table] if a is None or b is None or a[field] != b[field]]
                for field in changed:
                    summary["fields"][field] = summary["fields"].get(field, 0) + 1
                if table == "order_items" and a and b and set(changed) & {"price", "freight_value"}:
                    revised_orders.add(key[0]); revised_items.add(key)
                emit("records:" + table, key, kind, a, b, {"fields": changed})
            report["structural_diff"][table] = summary
        quality = []
        for conn, log in zip(conns, statements):
            issues = {}
            for row in rows(conn, "SELECT * FROM quality_issues ORDER BY issue_id", [], log):
                key = canonical([row["table_name"], json.loads(row["business_key"]), row["field_name"], row["reason"]])
                issues[key] = {"semantic": quality_signature(row), "evidence": row}
            quality.append(issues)
        for key in sorted(quality[0].keys() | quality[1].keys()):
            a, b = quality[0].get(key), quality[1].get(key)
            if a and b and a["semantic"] == b["semantic"]:
                continue
            report["quality_diff"]["changed"] += 1
            emit("quality", json.loads(key), change_kind(a, b), a, b)
            if len(report["quality_diff"]["preview"]) < 100:
                report["quality_diff"]["preview"].append({"key": json.loads(key), "before": a, "after": b})
        business = {"changed_orders": 0, "unknown_orders": 0, "payment_equivalent": True, "payment_projection_equal": True,
                    "revised_item_orders": len(revised_orders), "revised_item_keys": len(revised_items),
                    "payment_revision_mismatch": 0, "amount_revision_only": True}
        sources = [rows(conn, ORDER_PROJECTION, [], log) for conn, log in zip(conns, statements)]
        for key, a, b in aligned(*sources, key=lambda r: r["order_id"]):
            if a is None or b is None or any(a[field] != b[field] for field in ("payment_amount", "payment_known", "payment_missing")):
                business["payment_projection_equal"] = False
            if a is None or b is None or a["payment_amount"] is None or b["payment_amount"] is None or a["payment_amount"] != b["payment_amount"]:
                business["payment_equivalent"] = False
            if (a and any(a[m] is None for m in ("sales", "freight", "payment_amount"))) or (b and any(b[m] is None for m in ("sales", "freight", "payment_amount"))):
                business["unknown_orders"] += 1
            if a != b:
                business["changed_orders"] += 1
                delta = {m: numeric_delta(a[m], b[m]) if a and b else None for m in ("sales", "freight", "payment_amount")}
                complete = all(v is not None for v in delta.values())
                if complete and delta["payment_amount"] != delta["sales"] + delta["freight"]:
                    business["payment_revision_mismatch"] += 1
                emit("orders", key, change_kind(a, b), a, b, {"delta": delta, "payment_revision_matches": delta["payment_amount"] == delta["sales"]+delta["freight"] if complete else None})
        structural = report["structural_diff"]
        unchanged = lambda t: not any(structural[t][k] for k in ("added", "removed", "changed"))
        business["d2_equivalent"] = comparable and all(unchanged(t) for t in SCHEMA if t != "order_payments") and business["payment_projection_equal"] and report["quality_diff"]["changed"] == 0
        business["amount_revision_only"] = comparable and all(unchanged(t) for t in SCHEMA if t not in {"order_items", "order_payments"}) and not any(structural[t][k] for t in ("order_items", "order_payments") for k in ("added", "removed")) and set(structural["order_items"]["fields"]) <= {"price", "freight_value"} and set(structural["order_payments"]["fields"]) <= {"payment_value"} and report["quality_diff"]["changed"] == 0
        business["amount_revision_only"] = business["amount_revision_only"] and bool(revised_items or structural["order_payments"]["changed"])
        report["business_diff"] = business
        report["scope_changes"] = {field: [scope_contract(plans[0])[field], scope_contract(plans[1])[field]]
            for field in scope_contract(plans[0]) if scope_contract(plans[0])[field] != scope_contract(plans[1])[field]}
        comparable = comparable and not report["scope_changes"]
        display_sets = []
        for conn, plan, log in zip(conns, plans, statements):
            display_sets.append({canonical(r.get("entity_id", "all")) for r in read_analysis(conn, plan, log)}
                                if plan.intent in {"rank", "contribution"} or plan.min_group_count > 1 else None)
        period_results = {}
        for role in ("current", "previous"):
            if role == "previous" and not all(plan.comparison_range for plan in plans):
                continue
            current_plans = [p.model_copy(update={"intent": "aggregate", "time_range": p.time_range if role == "current" else p.comparison_range,
                "comparison_range": None, "min_group_count": 1}) for p in plans]
            metrics = plans[0].metric_ids
            if not metrics:
                continue
            results = [read_analysis(conn, p, log) for conn, p, log in zip(conns, current_plans, statements)]
            sample_maps = [review_samples(conn, p, log) if set(metrics) & {"avg_review", "low_score_rate"} else {}
                           for conn, p, log in zip(conns, current_plans, statements)]
            by_key = [{canonical(r.get("entity_id", "all")): r for r in result} for result in results]
            memberships = {m: compare_members(conns, current_plans, m, statements, emit, role, order_sets) for m in metrics if m in {"sales", "payment_amount", "order_count"}}
            for key in sorted(by_key[0].keys() | by_key[1].keys()):
                a, b = by_key[0].get(key), by_key[1].get(key)
                entity = (a or b).get("entity_id", "all")
                for metric in metrics:
                    before, after = value_state(a, metric), value_state(b, metric)
                    delta = numeric_delta(before["value"], after["value"]) if comparable else None
                    bridge = memberships.get(metric, {}).get(entity)
                    reconciled = None if not comparable or delta is None or not bridge or bridge["delta"] is None else delta == bridge["delta"]
                    result = {"period": role, "entity_id": entity, "entity_name": (a or b).get("entity_name", "全站"),
                        "group_values": {column: (a or b)[column] for column in result_columns(plans[0] if a is not None else plans[1])},
                        "metric": metric, "before": before, "after": after,
                        "delta": delta, "comparable": comparable, "membership": bridge, "reconciled": reconciled,
                        "displayed_before": key in display_sets[0] if display_sets[0] is not None else a is not None,
                        "displayed_after": key in display_sets[1] if display_sets[1] is not None else b is not None,
                        "samples_before": {k: v for k, v in (a or {}).items() if k in {"sample_orders", "delayed_orders", "reviewed_orders"}} | sample_maps[0].get(entity, {}),
                        "samples_after": {k: v for k, v in (b or {}).items() if k in {"sample_orders", "delayed_orders", "reviewed_orders"}} | sample_maps[1].get(entity, {})}
                    period_results[(role, key, metric)] = result
                    report["metric_comparisons"].append(result)
        if all(p.comparison_range for p in plans) and "month" not in dimensions(plans[0]):
            for key, metric in {(key, metric) for _, key, metric in period_results if metric in ADDITIVE}:
                for role in ("current", "previous"):
                    if (role, key, metric) not in period_results:
                        other = period_results[("previous" if role == "current" else "current", key, metric)]
                        absent = {"period": role, "entity_id": other["entity_id"], "entity_name": other["entity_name"], "group_values": other["group_values"], "metric": metric,
                            "before": value_state(None, metric), "after": value_state(None, metric),
                            "displayed_before": other["displayed_before"], "displayed_after": other["displayed_after"],
                            "delta": Decimal(0) if comparable else None, "comparable": comparable, "membership": None, "reconciled": None}
                        period_results[(role, key, metric)] = absent
                        report["metric_comparisons"].append(absent)
        for (role, key, metric), current in list(period_results.items()):
            if metric not in ADDITIVE:
                continue
            if role != "current" or ("previous", key, metric) not in period_results:
                continue
            previous = period_results[("previous", key, metric)]
            values = [previous["before"]["value"], current["before"]["value"], previous["after"]["value"], current["after"]["value"]]
            valid = comparable and all(value is not None for value in values)
            a3, a4, b3, b4 = values
            left = (b4-b3)-(a4-a3) if valid else None
            right = (b4-a4)-(b3-a3) if valid else None
            report["reconciliation"].append({"entity_id": current["entity_id"], "metric": metric, "values": values,
                "old_period_change": a4-a3 if a3 is not None and a4 is not None else None,
                "new_period_change": b4-b3 if b3 is not None and b4 is not None else None,
                "revision_effect": left, "revision_difference": right, "verified": left == right if valid else None})
        business["d2_equivalent"] = business["d2_equivalent"] and comparable and bool(report["metric_comparisons"]) and all(r[side]["state"] != "unknown" for r in report["metric_comparisons"] for side in ("before", "after"))
        business["d2_equivalent"] = business["d2_equivalent"] and all(r["before"]["value"] == r["after"]["value"] for r in report["metric_comparisons"])
    report.update(elapsed_ms=round((time.perf_counter()-started)*1000), statements=statements,
                  sql_counts=[len(log) for log in statements], csv_file=str(directory / "differences.csv"))
    (directory / "diff.json").write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
    return report


def _worker(paths, plans, directory, comparable, pipe):
    try:
        compare_snapshots(paths, [AnalysisPlan.model_validate(p) for p in plans], directory, comparable)
        pipe.send({"status": "completed"})
    except (duckdb.Error, ValueError) as error:
        pipe.send({"status": "failed", "error": str(error)})
    finally:
        pipe.close()


def run_diff(paths, plans, directory, comparable=True, timeout=30):
    context = mp.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=([str(p) for p in paths], [p.model_dump(mode="json") for p in plans], str(directory), comparable, writer))
    process.start()
    writer.close()
    try:
        if not reader.poll(timeout):
            return {"status": "timeout", "error": f"差异扫描超过 {timeout} 秒，已终止；已完成的两侧分析保留"}
        try:
            status = reader.recv()
        except EOFError:
            return {"status": "failed", "error": "差异子进程中断；已完成的两侧分析保留"}
        return json.loads((Path(directory) / "diff.json").read_text(encoding="utf-8"), parse_float=Decimal) if status["status"] == "completed" else status
    finally:
        if process.is_alive():
            process.terminate()
        process.join()
        process.close()
        reader.close()
