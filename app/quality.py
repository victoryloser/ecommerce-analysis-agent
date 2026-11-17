"""共享归并与质量证据；报表金额资格由绑定的计算规则决定。"""
import json
from decimal import Decimal
from collections import defaultdict

from app.source import PRIMARY_KEYS, SCHEMA

QUALITY_RULE_VERSION = "p0-amount-v1"
ISSUE_SCHEMA = {"issue_id": "INTEGER", "table_name": "VARCHAR", "order_id": "VARCHAR",
                "business_key": "VARCHAR", "field_name": "VARCHAR", "reason": "VARCHAR",
                "source_rows": "VARCHAR", "candidates": "VARCHAR"}
QUERY_SCHEMA = {**SCHEMA, "quality_issues": ISSUE_SCHEMA}
AMOUNT_FIELDS = {"order_items": {"price", "freight_value"}, "order_payments": {"payment_value"}}


def normalize_rows(table, rows):
    columns = list(SCHEMA[table])
    keys = [columns.index(key) for key in PRIMARY_KEYS[table]]
    grouped = defaultdict(list)
    for number, row in enumerate(rows, 2):
        grouped[tuple(row[index] for index in keys)].append((number, row))
    normalized, issues = [], []
    for key, records in grouped.items():
        unique = list(dict.fromkeys(tuple(row) for _, row in records))
        source_rows = json.dumps([number for number, _ in records])
        def issue(field, reason):
            issues.append((table, str(key[0]), json.dumps(key, default=str), field, reason,
                           source_rows, json.dumps(unique, default=str, ensure_ascii=False)))
        if len(records) > len(unique):
            issue("*", "exact_duplicate")
        merged = list(unique[0])
        for index, field in enumerate(columns):
            values = {row[index] for row in unique}
            if len(values) > 1:
                if field not in AMOUNT_FIELDS.get(table, set()):
                    raise ValueError(f"{table} {key} 的 {field} 冲突；当前规则仅归并金额冲突")
                merged[index] = None
                issue(field, "amount_conflict")
            elif merged[index] is None and field in AMOUNT_FIELDS.get(table, set()):
                issue(field, "missing_value")
        normalized.append(merged)
    return normalized, issues


def decode_metric_quality(raw):
    quality = json.loads(raw, parse_float=Decimal)
    for q in quality.values():
        if q and isinstance(q.get("known_value"), str):
            q["known_value"] = Decimal(q["known_value"])
    return quality


def decode_quality(columns, rows):
    if "__quality" not in columns:
        return columns, [list(row) for row in rows], []
    index = columns.index("__quality")
    quality = [decode_metric_quality(row[index]) for row in rows]
    return columns[:index] + columns[index+1:], [list(row[:index]) + list(row[index+1:]) for row in rows], quality


def exact_sum(column):
    return f"CASE WHEN COUNT(*)=COUNT({column}) THEN SUM({column}) END"


def calculation_determined(q):
    return q.get("calculation_status", "determined" if q.get("completeness") == "complete" else "insufficient_data") == "determined"


def quality_note(row):
    from app.answers import label
    parts = []
    for metric, q in row.items():
        if not q:
            continue
        if q.get("amount_basis") == "policy_eligible" and q["completeness"] != "complete":
            state = "已知准入范围可计算" if calculation_determined(q) else "准入范围仍有金额或归属未知"
            parts.append(f"{label(metric)}：{state}；确定准入 {q['policy_eligible_order_count']} 笔，确定排除 {q['excluded_order_count']} 笔，排除候选归属未知 {q['excluded_candidate_order_count']} 笔；不代表完整总体金额")
        elif q["completeness"] != "complete":
            known = q["known_value"] if q["known_count"] else "暂无可确认值"
            parts.append(f"{label(metric)}：总值未知，已确认部分 {known}；数值未知 {q['value_unknown_count']}，归属未知 {q['membership_unknown_count']}，缺记录 {q.get('missing_record_count', 0)}，影响订单 {q['affected_order_count']}")
        if q.get("group_eligibility") == "uncertain":
            parts.append("订单数门槛是否满足仍未知，不能确认名次")
    return "；".join(dict.fromkeys(parts)) or "完整"
