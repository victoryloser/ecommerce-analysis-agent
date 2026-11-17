"""Business obligations, independent numeric checks and delivered-answer checks."""
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
import re

from app.models import AnalysisPlan, QueryResult
from app.round2_suite import contract

JUDGE_VERSION = "round2-judge-1"


def normalized_scope(plan):
    conditions = {}
    def add(field, values):
        values = frozenset(str(v) for v in values)
        conditions[field] = conditions[field] & values if field in conditions else values
    def visit(node):
        if node.negate or (node.children and node.conjunction != "all"):
            raise ValueError("复杂 OR/NOT 范围需要人工核验")
        if node.children:
            for child in node.children:
                visit(child)
        elif node.operator in {"eq", "in"}:
            add(node.field, node.values)
        else:
            raise ValueError("当前契约不能证明该条件等价")
    add("order_status", plan.order_statuses)
    for f in plan.filters:
        add(f.field, f.values)
    for node in (plan.order_where, plan.item_where):
        if node:
            visit(node)
    if plan.selected_order_id:
        add("order_id", [plan.selected_order_id])
    return (plan.dataset_id, plan.metadata_version, plan.time_field, plan.time_range, conditions)


def same_scope(actual, expected, *, comparison=True):
    assert normalized_scope(actual) == normalized_scope(expected), "生效时间、状态或实体范围不符"
    if comparison:
        assert actual.comparison_range == expected.comparison_range, "比较期不符"


def equivalent_grouping(actual, expected):
    if not actual.group_by and actual.dimension == expected.dimension:
        return True
    if expected.dimension != "total" or expected.group_by:
        return False
    # A group fixed to one value by the required scope is the same population.
    required = normalized_scope(expected)[-1]
    fields = {"category": "category_id", "seller": "seller_id", "customer_state": "customer_state"}
    dims = actual.group_by or [actual.dimension]
    return all(dim in fields and len(required.get(fields[dim], ())) == 1 for dim in dims)


def merged_periods(periods):
    merged = []
    for start, end in sorted((p.start, p.end) for p in periods):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def equal_value(actual, expected, field):
    if isinstance(expected, (datetime, date)):
        assert str(actual).replace("T", " ") == str(expected).replace("T", " "), f"{field}: {actual} != {expected}"
    elif isinstance(expected, (Decimal, float)):
        expected = Decimal(str(expected))
        assert actual is not None, f"{field} 应为 {expected}，实际未知"
        observed = Decimal(str(actual))
        tolerance = Decimal("0.00005") if "rate" in field else Decimal(0)
        assert abs(observed-expected) <= tolerance, f"{field}: {actual} != {expected}"
    else:
        assert actual == expected, f"{field}: {actual} != {expected}"


def compare_rows(result, expected, *, ordered=False):
    assert result.status in {"ok", "empty"}, f"查询未完成：{result.status}"
    assert not result.truncated and result.row_offset == 0, "结果截断或不是首页"
    actual = [dict(zip(result.columns, row)) for row in result.rows]
    assert len(actual) == len(expected), f"行数 {len(actual)} != {len(expected)}"
    keys = next((keys for keys in [("issue_type", "business_key"), ("issue_type",), ("entity_id",), ("order_id",)]
                 if expected and all(k in expected[0] for k in keys)), ())
    if keys and not ordered:
        sort = lambda r: tuple(str(r[k]) for k in keys)
        actual = sorted(actual, key=sort)
        expected = sorted(expected, key=sort)
    for row, wanted in zip(actual, expected):
        for field, value in wanted.items():
            if field == "__quality":
                continue
            assert field in row, f"缺少列：{field}"
            equal_value(row[field], value, field)
    if any("__quality" in row for row in expected):
        assert len(result.quality) == len(result.rows), "缺少金额完整性证据"
        raw = [dict(zip(result.columns, row)) for row in result.rows]
        indexed = {tuple(str(r[k]) for k in keys): q for r, q in zip(raw, result.quality)} if keys else {(): result.quality[0]}
        for wanted in expected:
            quality = indexed[tuple(str(wanted[k]) for k in keys)]
            for metric, fields in wanted.get("__quality", {}).items():
                assert metric in quality, f"缺少 {metric} 质量状态"
                for field, value in fields.items():
                    equal_value(quality[metric].get(field), value, metric+"."+field)


def _render(field, value):
    if value is None:
        return "不可计算/无数据"
    if isinstance(value, Decimal) or (isinstance(value, (int, float)) and any(w in field for w in ("sales", "freight", "payment", "rate"))):
        value = Decimal(str(value))
        scale = 100 if "rate" in field else 1
        return str((value*scale).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + ("%" if scale == 100 else "")
    return str(value)


def answer_tables(text):
    lines = text.replace("**", "").splitlines()
    tables = []
    headers = None
    for line in lines:
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]+", c) for c in cells):
                continue
            if headers is None:
                headers = cells
            else:
                tables.append(dict(zip(headers, cells)))
        else:
            headers = None
    return tables


def check_answer_values(text, plan, expected):
    """Check labeled table cells / prose; finding an unrelated number is insufficient."""
    from app.answers import label
    tables = answer_tables(text)
    aliases = {}
    if plan.intent == "compare" and len(plan.metric_ids) == 1:
        metric = plan.metric_ids[0]
        aliases = {metric+"_current": "current_value", metric+"_previous": "previous_value",
                   metric+"_change": "absolute_change", metric+"_rate": "change_rate"}
        expected = [{**row, **{target: row[source] for target, source in aliases.items()}} for row in expected]
    for row in expected:
        identity = row.get("entity_id")
        for field, value in row.items():
            if field not in {"sales", "freight", "order_count", "repeat_customers", "sales_current", "sales_previous", "sales_change", "sales_rate",
                             "freight_current", "freight_previous", "order_count_current", "order_count_previous", "contribution_rate", "rank_no"}:
                continue
            wanted = _render(field, value)
            if value is None and "rate" in field:
                denominator = row.get("total_change") if field == "contribution_rate" else row.get("sales_previous")
                if denominator == 0:
                    wanted = "未定义（分母为0）"
            matches = [r for r in tables if identity is None or str(identity) in r.values()]
            found = any(r.get(label(field)) == wanted for r in matches)
            if field in aliases:
                found |= any(r.get(label(aliases[field])) == wanted for r in matches)
            if field.endswith(("_previous", "_current")) and identity is None:
                base, suffix = field.rsplit("_", 1)
                period = plan.comparison_range if suffix == "previous" else plan.time_range
                found |= any(r.get(label(base)) == wanted and str(period.start) in r.get("期间", "") for r in tables)
            names = {"sales_change": "商品额差额", "sales_rate": "环比", "order_count": "订单总数", "repeat_customers": "复购自然客户数"}
            if identity is None:
                found |= any(re.search(re.escape(name)+r"[：: ]*"+re.escape(wanted)+r"(?![\d.])", text)
                             for name in (label(field), names.get(field, label(field))))
            assert found, f"实际回答缺少或错误：{identity or '总体'} {label(field)}={wanted}"


def judge(case_id, manifest, oracle, evidence, answer_text, *, turn_id=None, reused_ids=()):
    checks = []
    def check(name, action, refs=()):
        try:
            action()
            checks.append({"name": name, "passed": True, "evidence_ids": list(refs)})
        except (AssertionError, ValueError, KeyError, TypeError) as error:
            checks.append({"name": name, "passed": False, "reason": str(error), "evidence_ids": list(refs)})
    def require(condition, message):
        assert condition, message
    target = contract(manifest, case_id)
    records = evidence.get("steps", [evidence])
    records = [r for r in records if r and r.get("result")]
    pairs = [(AnalysisPlan.model_validate(r["plan"]), QueryResult.model_validate(r["result"]), r) for r in records]
    check("execution", lambda: require(bool(pairs) and all(t["status"] in {"completed", "unsupported"} for t in evidence.get("tasks", [])), "任务未全部完成"))
    from app.calculation import ROUND2_RULE
    check("amount_rule", lambda: require(manifest.get("quality_rule_version") == ROUND2_RULE, "快照未绑定正式金额规则"))
    check("lineage", lambda: require(all(r.get("dataset_id") == manifest["dataset_id"] and
        r.get("database_sha256") == manifest["database_sha256"] and
        r.get("metadata_sha256") == manifest["metadata_sha256"] and
        r.get("quality_rule_version") == manifest.get("quality_rule_version") and
        (turn_id is None or r.get("turn_id") == turn_id or r.get("record_id") in reused_ids)
        for _, _, r in pairs), "结果来自其他快照或回合"))
    for plan, result, record in pairs:
        def numeric(plan=plan, result=result):
            if plan.diagnosis == "order_candidate" and plan.intent == "diagnose":
                eligible = oracle.details(plan.model_copy(update={"detail_level": "items"}))
                ids = {r["order_id"] for r in eligible if r["membership_confirmed"] is True and r["order_id"] not in oracle.excluded_orders}
                rows = [dict(zip(result.columns, row)) for row in result.rows]
                require(result.status == "ok" and len(rows) == 1 and rows[0]["order_id"] in ids, "候选不属于有效订单")
            else:
                compare_rows(result, oracle.evaluate(plan, with_quality=True))
        check("source_values", numeric, [record.get("record_id")])

    matched = []
    def obligations():
        if case_id == "B2":
            rows = []
            scopes = {}
            for plan, result, record in pairs:
                if plan.intent != "diagnose" or plan.diagnosis != "quality_rows":
                    continue
                # Union query or separate monthly queries both satisfy the contract.
                periods = [plan.comparison_range] if plan.quality_scope == "previous" else [plan.time_range]
                if plan.quality_scope == "comparison_union" and plan.comparison_range:
                    periods.append(plan.comparison_range)
                for period in periods:
                    same_scope(plan.model_copy(update={"time_range": period}), target.model_copy(update={"time_range": period}), comparison=False)
                    for kind in plan.quality_types:
                        scopes.setdefault(kind, []).append(period)
                rows.extend(dict(zip(result.columns, r)) for r in result.rows)
                matched.append((plan, result, record))
            require(set(scopes) == set(target.quality_types) and all(merged_periods(periods) == merged_periods([target.time_range, target.comparison_range])
                for periods in scopes.values()), "遗漏基期、异常类型或查验期间越界")
            expected = oracle.evaluate(target)
            observed = {(r["issue_type"], r["business_key"]): r for r in rows}
            require(len(observed) == len(expected), "异常业务键不符")
            for wanted in expected:
                row = observed[(wanted["issue_type"], wanted["business_key"])]
                for key, value in wanted.items():
                    equal_value(row[key], value, key)
                require(wanted["order_id"] in answer_text and wanted["business_key"] in answer_text, "回答遗漏异常业务键")
            return
        metrics = set()
        for plan, result, record in pairs:
            if plan.intent != target.intent or not equivalent_grouping(plan, target):
                continue
            same_scope(plan, target)
            require(plan.min_group_count == 1 and not plan.rank_within and not plan.contribution_within, "不应增加分组门槛或分区")
            if case_id == "A2":
                require(plan.limit_mode == "all" and plan.contribution_direction == "increase", "必须全品类按差额降序")
            if case_id == "A4":
                require(plan.top_n == 3, "应保留第三名并列")
            wanted_plan = target.model_copy(update={"metric_ids": plan.metric_ids})
            wanted = oracle.evaluate(wanted_plan, with_quality=True)
            compare_rows(result, wanted)
            if case_id in {"A2", "A4"}:
                field = "sales_change" if case_id == "A2" else "sales"
                values = [dict(zip(result.columns, r))[field] for r in result.rows]
                require(values == sorted(values, reverse=True), "未按金额降序展示")
            check_answer_values(answer_text, wanted_plan, wanted)
            metrics.update(plan.metric_ids)
            matched.append((plan, result, record))
        require(set(target.metric_ids) <= metrics, f"缺少业务指标：{set(target.metric_ids)-metrics}")
        if case_id == "A4":
            ranked = oracle.evaluate(target)
            champions = {r["entity_id"] for r in ranked if r["rank_no"] == 1}
            audits = [(p, r, e) for p, r, e in pairs if p.intent == "diagnose" and p.diagnosis == "order_audit"]
            require(bool(audits), "缺少整单核查")
            for plan, result, record in audits:
                require(plan.audit_seller_id in champions, "不是冠军商家")
                require(plan.selected_order_id not in oracle.excluded_orders, "不能选择金额未准入订单")
                same_scope(plan.model_copy(update={"selected_order_id": None}), target)
                require(len(result.rows) == 1, "缺少选中订单")
                row = dict(zip(result.columns, result.rows[0]))
                require(any(i["seller_id"] == plan.audit_seller_id for i in oracle.items[plan.selected_order_id]), "订单不属于冠军")
                for field, name in (("seller_sales", "选中商家商品小计"), ("order_sales", "整单商品额"), ("payment_amount", "整单支付额")):
                    require(re.search(r"\|\s*"+name+r"\s*\|\s*"+re.escape(_render(field, row[field]))+r"\s*\|", answer_text), "回答遗漏或错写"+name)
                items = [(p, r, e) for p, r, e in pairs if p.intent == "details" and p.detail_level == "items" and p.selected_order_id == plan.selected_order_id]
                require(bool(items), "缺少全部商品明细")
                for p, r, e in items:
                    same_scope(p.model_copy(update={"selected_order_id": None}), target)
                    require(p.audit_seller_id == plan.audit_seller_id, "明细商家标记不符")
                    from app.answers import label
                    presented = answer_tables(answer_text)
                    for item in oracle.evaluate(p)[:30]:
                        values = {label(f): _render(f, item[f]) for f in ("order_item_id", "product_name", "seller_id", "price", "freight_value")}
                        values[label("belongs_to_selected_seller")] = "是" if item["belongs_to_selected_seller"] else "否"
                        require(any(all(row.get(k) == v for k, v in values.items()) for row in presented), "回答遗漏或错写商品明细")
                matched.extend(audits + items)
    check("business_obligations_and_answer", obligations, [r.get("record_id") for r in records])
    def explanation():
        if case_id in {"A1", "B1"}:
            require(manifest["name"] in answer_text or manifest["dataset_id"] in answer_text, "未说明数据版本")
        if case_id == "A1":
            require("分别" in answer_text and "订单" in answer_text and "明细" in answer_text and any(r.get("sql") for r in records), "缺少计算安排和查询依据")
        if case_id == "A2":
            require(bool(re.search(r"不能证明促销因果|不足以把变化归因于促销|无法.*促销.*因果", answer_text)) and
                    any(w in answer_text for w in ("对照", "投放", "活动")), "未说明因果证据限制")
        if case_id == "B3":
            require("无法计算促销" in answer_text and all(w in answer_text for w in ("成本", "费用", "因果")), "未说明利润所缺证据")
            excluded = len({r["order_id"] for r in oracle.details(target)} & oracle.excluded_orders)
            if excluded:
                require(f"确定排除 {excluded} 笔" in answer_text and "不代表完整总体金额" in answer_text, "未交代金额缺口")
        if case_id == "B2":
            require(all(s in answer_text for s in ("只保留一份", "整单暂不计入", "订单数保留", "支付额未知", "拆分支付保留")), "异常处理或影响说明不完整")
        if case_id == "B4":
            require("customer_unique_id" in answer_text and "不同" in answer_text, "未交代自然客户去重粒度")
    check("explanation", explanation)
    return {"passed": all(c["passed"] for c in checks), "checks": checks,
            "judge_version": JUDGE_VERSION, "manual_review": ["表达清晰度", "解释是否重复"]}
