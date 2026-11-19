"""每个指标独立刻度的可点击图表；数值及稳定ID均来自当前查询页。"""
from html import escape

from app.answers import cell, label
from app.models import AnalysisPlan
from app.presentation import display_evidence, _number

CLICK_SCRIPT = """element.addEventListener('click', function(event) {
    const point = event.target.closest('[data-group-id]');
    if (point) trigger('select', {index: 0, value: point.dataset.groupId, selected: true});
});"""


def numeric_columns(evidence: dict) -> list[str]:
    evidence = display_evidence(evidence)
    result = evidence.get("result")
    if not result or result["status"] not in {"ok", "empty"}:
        return []
    return [column for index, column in enumerate(result["columns"])
            if not column.endswith(("_id", "_name")) and column not in {"month", "rank_no"}
            and (any(row[index] is not None for row in result["rows"]) or any(column in q for q in result.get("quality", [])))
            and all(row[index] is None or _number(row[index]) for row in result["rows"])]


def render_charts(evidence: dict, columns: list[str]) -> str:
    evidence = display_evidence(evidence)
    allowed = numeric_columns(evidence)
    if not allowed:
        return ""
    result = evidence["result"]
    plan = AnalysisPlan.model_validate(evidence["plan"]) if evidence["type"] == "query" else None
    rows = [dict(zip(result["columns"], row)) for row in result["rows"][:50]]
    sections = []
    for column in columns:
        if column not in allowed:
            continue
        related = [column]
        for suffix, other in (("_current", "_previous"), ("_previous", "_current")):
            if column.endswith(suffix) and column[:-len(suffix)]+other in columns:
                related.append(column[:-len(suffix)]+other)
        values = [float(row[name]) for name in related for row in rows if row[name] is not None]
        scale = max([abs(value) for value in values] + [1e-12])
        bars = []
        for index, row in enumerate(rows):
            value = row[column]
            if value is None:
                group = str(row.get("entity_name", row.get("entity_id", index+1)))
                bars.append(f'<div class="chart-unknown">{escape(group)}：未知 / 不可计算</div>')
                continue
            text = cell(column, value, plan) if plan else str(value)
            group = str(row.get("entity_name", row.get("entity_id", index+1)))
            group_id = str(row.get("entity_id", "all"))
            tag = "button" if plan and plan.intent != "details" and not column.endswith("_previous") and not column.startswith("previous_") else "div"
            attrs = f' data-group-id="{escape(group_id, quote=True)}" type="button"' if tag == "button" else ""
            width = abs(float(value)) / scale * 100
            tone = "negative" if float(value) < 0 else "positive"
            bars.append(f'<{tag}{attrs} class="chart-row" aria-label="{escape(group+"："+text, quote=True)}">'
                        f'<span>{escape(group)}</span><span class="chart-track"><span class="{tone}" style="width:{width:.4f}%"></span></span>'
                        f'<span>{escape(text)}</span></{tag}>')
        sections.append(f'<section><h3>{escape(label(column))}</h3>'+"".join(bars)+"</section>")
    note = ("当前页前50组；" if len(result["rows"]) > 50 else "") + "不同指标独立刻度，同一指标本期与基期共享刻度；负值为橙色。点击本期或变化额下钻当前期间，基期仅展示。"
    return '<div class="desk-charts">'+"".join(sections)+"</div><p>"+note+"</p>"
