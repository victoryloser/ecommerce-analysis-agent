"""Posted refund ledger. Receipt amounts and reversals are positive on input."""

import csv
import hashlib
from io import StringIO
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import re

from app.calculation import price_conflict_sql
from app.models import AnalysisPlan
from app.semantic import validate_plan
from app.snapshot_diff import read_analysis
from app.weekly import business_zone, open_snapshot

REFUND_RULE = "refund-posted-v1"
FIELDS = {"refund_id", "order_id", "posted_at", "status", "currency"}
AMOUNTS = ("goods_amount", "freight_amount", "total_amount")


def parse_ledger(raw, timezone):
    zone = business_zone(timezone)
    reader = csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""))
    headers = reader.fieldnames or []
    fields = set(headers)
    if len(fields) != len(headers):
        raise ValueError("退款 CSV 含重复列名，请核对金额列及表头")
    if not FIELDS <= fields or not fields.intersection(AMOUNTS):
        raise ValueError("退款 CSV 缺少流水号、订单号、入账时间、状态、币种或金额列")
    records, instants = {}, {}
    for line, row in enumerate(reader, 2):
        if None in row or None in row.values():
            raise ValueError(f"第 {line} 行列数与表头不一致")
        key, oid, status, currency = (row[k].strip() for k in ("refund_id", "order_id", "status", "currency"))
        if (
            not key
            or not oid
            or status not in {"success", "reversal", "pending", "failed"}
            or not re.fullmatch("[A-Z]{3}", currency)
        ):
            raise ValueError(f"第 {line} 行退款键、状态或币种无效")
        moment = row["posted_at"].strip()
        if not moment and status in {"success", "reversal"}:
            raise ValueError(f"流水 {key} 缺少实际入账时间")
        timestamp = datetime.fromisoformat(moment) if moment else None
        if timestamp:
            timestamp = timestamp.astimezone(zone) if timestamp.tzinfo else timestamp.replace(tzinfo=zone)
        instant = timestamp.astimezone(UTC) if timestamp else None
        amounts = {}
        for field in AMOUNTS:
            value = (row.get(field) or "").strip()
            try:
                amount = Decimal(value) if value else None
                if amount is not None and (
                    not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal(".01"))
                ):
                    raise ValueError(f"流水 {key} 需要非负且精确到分的金额")
            except InvalidOperation as err:
                raise ValueError(f"流水 {key} 的金额无效") from err
            amounts[field] = amount.quantize(Decimal(".01")) if amount is not None else None
        goods, freight, total = (amounts[k] for k in AMOUNTS)
        if total is not None and freight is not None and goods is None:
            goods = total - freight
        if total is not None and goods is not None and freight is None:
            freight = total - goods
        if goods is not None and freight is not None:
            if min(goods, freight) < 0 or total is not None and goods + freight != total:
                raise ValueError(f"流水 {key} 商品及运费与合计不符")
            total = goods + freight
        if status in {"success", "reversal"} and goods is None and total is None:
            raise ValueError(f"流水 {key} 需要商品退款额或整单退款额")
        entry = dict(
            id=key,
            order=oid,
            posted=timestamp.replace(tzinfo=None).isoformat() if timestamp else None,
            status=status,
            currency=currency,
            goods=goods,
            freight=freight,
            total=total,
            original=(row.get("original_refund_id") or "").strip(),
        )
        if key in records and (records[key] != entry or instants[key] != instant):
            raise ValueError(f"同一流水 {key} 有冲突记录")
        records[key] = entry
        instants[key] = instant
    reversed_ids = set()
    for r in records.values():
        if r["status"] != "reversal":
            continue
        orig = records.get(r["original"])
        if (
            not orig
            or orig["status"] != "success"
            or instants[orig["id"]] > instants[r["id"]]
            or orig["id"] in reversed_ids
            or any(orig[k] != r[k] for k in ("order", "currency", "goods", "freight", "total"))
        ):
            raise ValueError(f"撤销流水 {r['id']} 需对应一笔同订单同币种、同金额且尚未撤销的先前成功流水")
        reversed_ids.add(orig["id"])
    return list(records.values()), hashlib.sha256(raw).hexdigest()


def ledger_for(db, dataset, period):
    ledger = db.execute("SELECT * FROM refund_ledgers WHERE dataset=?", (dataset,)).fetchone()
    if not ledger or ledger["start"] > str(period.start) or ledger["end"] < str(period.end):
        raise ValueError(f"请先交付并确认覆盖 [{period.start}, {period.end}) 的退款流水")
    return ledger


def reconcile(executor, ledger, period):
    manifest = executor.manifest
    plan = AnalysisPlan(
        dataset_id=manifest["dataset_id"],
        metadata_version=manifest["metadata_version"],
        intent="aggregate",
        metric_ids=["sales"],
        time_range=period,
    )
    validate_plan(plan, manifest)
    statements = []
    with open_snapshot(executor) as conn:
        sales = read_analysis(conn, plan, statements, executor.context)[0]
        order_status = dict(conn.execute("SELECT order_id,order_status FROM orders").fetchall())
        conflicts = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT order_id FROM quality_issues WHERE {price_conflict_sql()}"
            ).fetchall()
        }
        unknown = {
            r[0]
            for r in conn.execute("""SELECT o.order_id FROM orders o LEFT JOIN order_items i USING(order_id)
                                                 WHERE i.price IS NULL""").fetchall()
        }
    buckets = {}
    exceptions = []
    postings = []

    def bucket(currency):
        return buckets.setdefault(
            currency,
            {
                "known_goods_refunds": Decimal(0),
                "known_freight_refunds": Decimal(0),
                "known_refund_total": Decimal(0),
                "pending_count": 0,
                "posted_count": 0,
                "unknown_total_count": 0,
                "unknown_freight_count": 0,
            },
        )

    bucket("CNY")
    for r in ledger:
        if not r["posted"] or not (period.start.isoformat() <= r["posted"][:10] < period.end.isoformat()):
            continue
        b = bucket(r["currency"])
        if r["status"] in {"pending", "failed"}:
            b["pending_count"] += int(r["status"] == "pending")
            continue
        b["posted_count"] += 1
        sign = -1 if r["status"] == "reversal" else 1
        for field, target, missing in (
            ("freight", "known_freight_refunds", "unknown_freight_count"),
            ("total", "known_refund_total", "unknown_total_count"),
        ):
            if r[field] is None:
                b[missing] += 1
            else:
                b[target] += sign * Decimal(str(r[field]))
        reason = None
        if r["order"] not in order_status:
            reason = "订单未匹配"
        elif order_status[r["order"]] != "delivered":
            reason = "订单不属于已送达口径"
        elif r["order"] in conflicts or r["order"] in unknown:
            reason = "订单商品金额待核实"
        elif r["goods"] is None:
            reason = "整单退款未拆分"
        postings.append({**r, "attribution": reason or "可归属"})
        if reason:
            exceptions.append(
                {
                    "refund": r["id"],
                    "order": r["order"],
                    "currency": r["currency"],
                    "reason": reason,
                    "goods": r["goods"],
                    "total": r["total"],
                    "status": r["status"],
                }
            )
        else:
            b["known_goods_refunds"] += sign * Decimal(str(r["goods"]))
    for currency, b in buckets.items():
        b["unverified_count"] = sum(e["currency"] == currency for e in exceptions)
        b["eligible_sales"] = sales["sales"] if currency == "CNY" else None
        subtotal = sales["quality"]["sales"]["known_value"] if currency == "CNY" else None
        b["known_net"] = subtotal - b["known_goods_refunds"] if subtotal is not None else None
        b["net"] = (
            b["known_net"]
            if not b["unverified_count"] and sales["quality"]["sales"]["population_complete"]
            else None
        )
    return {
        "rule": REFUND_RULE,
        "period": period.model_dump(mode="json"),
        "currencies": buckets,
        "exceptions": exceptions,
        "postings": postings,
        "sales_quality": sales["quality"]["sales"],
        "snapshot": manifest["database_sha256"],
        "statements": statements,
    }
