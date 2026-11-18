"""表格、图表和导出共用当前查询证据，不重复查询或调用模型。"""
import csv
import json
import re
from datetime import date
from dataclasses import dataclass, field

from app.answers import LABELS, cell, label, plan_label
from app.config import RUNS_DIR
from app.models import AnalysisPlan, QueryResult
from app.grouping import dimensions


def example_questions(manifest):
    """示例问题自行写明年份，不依赖用户看不到的演示配置。"""
    if manifest.get("capabilities"):
        questions = ["全期已送达订单数是多少？"]
        if "sales" in manifest["capabilities"]["metrics"]:
            questions.append("按月查看全期已送达订单数及商品额")
        return questions
    year = manifest["practice"].get("year_context")
    questions = []
    for item in manifest["practice"]["questions"]:
        question = item["question"]
        if year and "年" not in question:
            question = re.sub(r"(?<!\d)(\d{1,2})月", rf"{year}年\1月", question)
        questions.append(question)
    questions = questions or ["全部数据中，已送达订单的商品销售额是多少？", "按月份查看商品销售额和订单量", "按品类查看商品销售额", "查看延迟率最高的商家"]
    return questions + ["按品类和商家联合列出全期商品销售额及订单数", "全期每个品类分别按商品销售额列出前三商家，保留并列", "按月份和品类查看全期商品销售额趋势",
                        "按品类与客户省份联合统计全期商品销售额、运费和订单数"]


@dataclass
class ResultView:
    headers: list[str] = field(default_factory=list)
    table: list[list[str]] = field(default_factory=list)
    chart: list[dict] = field(default_factory=list)
    chart_kind: str | None = None
    chart_title: str = ""
    chart_series: str | None = None
    csv_file: str | None = None
    evidence_file: str | None = None


def display_evidence(evidence: dict) -> dict:
    if evidence.get("type") == "workflow":
        focus = evidence.get("focus_step")
        if focus and focus <= len(evidence["steps"]):
            selected = evidence["steps"][focus-1]
            if (selected.get("result") or {}).get("status") in {"ok", "empty"}:
                return selected
        queries = [step for step in evidence["steps"] if step.get("type") == "query" and step["result"]["status"] in {"ok", "empty"}]
        return queries[-1] if queries else evidence
    return evidence


def present(evidence: dict, metric: str | None = None) -> ResultView:
    view = ResultView()
    if "record_id" not in evidence:
        return view
    original = evidence
    evidence = display_evidence(evidence)
    directory = RUNS_DIR / evidence["dataset_id"]
    view.evidence_file = str(directory / f"{original['record_id']}.json")
    if evidence["type"] not in {"query", "sql_exploration", "quality_details"}:
        return view
    result = QueryResult.model_validate(evidence["result"])
    if result.status not in {"ok", "empty"}:
        return view
    plan = AnalysisPlan.model_validate(evidence["plan"]) if evidence["type"] == "query" else None
    view.headers = [plan_label(c, plan) for c in result.columns] if plan else result.columns
    view.table = [[cell(c, v, plan, result.ratios[i] if result.ratios else None) if plan else ("NULL" if v is None else str(v)) for c, v in zip(result.columns, row)] for i, row in enumerate(result.rows)]
    if result.quality:
        from app.quality import quality_note
        view.headers.append("数据完整度")
        view.table = [row + [quality_note(q)] for row, q in zip(view.table, result.quality)]
    if plan and plan.audit_seller_id and not result.quality:
        visible = (["entity_id", "selected_seller_id", "seller_sales", "order_sales", "order_freight", "payment_amount", "payment_records", "amount_eligible"]
                   if plan.intent == "diagnose" and plan.diagnosis == "order_audit" else
                   ["entity_id", "order_item_id", "product_name", "seller_id", "price", "freight_value", "belongs_to_selected_seller", "amount_eligible"])
        indexes = [result.columns.index(c) for c in visible if c in result.columns]
        view.headers = [view.headers[i] for i in indexes]
        view.table = [[row[i] for i in indexes] for row in view.table]
    # CSV 保留原始列名、金额精度和0~1比例；JSON同时保存口径、期间与SQL。
    path = directory / f"{result.query_id}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(result.columns + (["__quality"] if result.quality else []))
        if result.quality:
            writer.writerows([*row, json.dumps(q, ensure_ascii=False, default=str)] for row, q in zip(result.rows, result.quality))
        else:
            writer.writerows(result.rows)
    view.csv_file = str(path)
    if not result.rows:
        return view
    if plan is None:
        numeric = [c for index, c in enumerate(result.columns) if all(v[index] is None or _number(v[index]) for v in result.rows) and any(v[index] is not None for v in result.rows)]
        if not numeric:
            return view
        metric = metric if metric in numeric else numeric[0]
        group_column = next((c for c in result.columns if c not in numeric), result.columns[0])
        for index, values in enumerate(result.rows):
            row = dict(zip(result.columns, values))
            if row[metric] is not None:
                view.chart.append({"分组": str(row[group_column]) if group_column != metric else str(index+1), "数值": float(row[metric]), "分组ID": str(row[group_column])})
        view.chart_kind = "bar"
        view.chart_title = label(metric) + "（自定义探索，当前页）"
        return view
    if plan.intent in {"details"} or (not dimensions(plan) and plan.intent != "diagnose"):
        return view
    candidates = [c for c in result.columns if c in plan.metric_ids or c.endswith(("_current", "_previous", "_change", "_rate")) or c in {"issue_count", "difference", "z_score"}]
    if not candidates:
        return view
    metric = metric if metric in candidates else (plan.metric_ids[0] if plan.metric_ids and plan.metric_ids[0] in candidates else candidates[0])
    ratio = metric in {"delay_rate", "low_score_rate", "contribution_rate"} or metric.endswith("_rate")
    if plan.group_by and "month" in dimensions(plan):
        view.chart_series = "品类"
        months = []
        current = plan.time_range.start.replace(day=1)
        while current < plan.time_range.end:
            months.append(current.strftime("%Y-%m"))
            current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
        data = [dict(zip(result.columns, values)) for values in result.rows]
        series = {r["category_id"]: r["category_name"] for r in data}
        points = {(r["month"], r["category_id"]): r for r in data}
        for category, name in series.items():
            for month in months:
                row = points.get((month, category))
                value = row[metric] if row else None
                view.chart.append({"分组": month, "品类": f"{name if name is not None else '未标注'} [{category}]",
                    "数值": float(value)*(100 if ratio else 1) if value is not None else None,
                    "分组ID": row["entity_id"] if row else None})
        view.chart_kind = "line"
        view.chart_title = label(metric) + "（按月、按品类；未显示或未确认的组合留空）"
        return view
    for values in result.rows:
        row = dict(zip(result.columns, values))
        group_name = str(row["entity_name"]) if row["entity_name"] is not None else "未标注"
        if row[metric] is not None:
            view.chart.append({"分组": group_name, "数值": float(row[metric]) * (100 if ratio else 1), **({"分组ID": row["entity_id"]} if plan.group_by else {})})
        else:
            view.chart.append({"分组": group_name + "（未知/不可计算）", "数值": None})
    view.chart_kind = "line" if plan.dimension == "month" else "bar"
    view.chart_title = label(metric) + ("（%）" if ratio else "")
    return view


def _number(value):
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False
