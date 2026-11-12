from decimal import ROUND_HALF_UP, Decimal
from datetime import timedelta

from app.models import AnalysisPlan, QueryResult
from app.semantic import METRICS, RELATIONSHIPS

LABELS = {
    "entity_id": "分组ID", "entity_name": "分组", "sales": "商品销售额（元）", "order_count": "已送达订单数",
    "payment_amount": "支付金额（元）", "delay_rate": "延迟率", "avg_review": "平均评分", "low_score_rate": "低分率",
    "repeat_customers": "复购自然客户数", "sample_orders": "已送达订单数", "delayed_orders": "延迟订单数",
    "missing_timing_orders": "时间缺失订单数", "reviewed_orders": "有评价订单数", "rank_no": "名次",
    "current_value": "本期", "previous_value": "基期", "absolute_change": "变化量", "change_rate": "变化率",
    "order_id": "订单ID", "item_rows": "商品明细行数", "payment_rows": "支付记录行数", "joined_rows": "直接关联行数",
    "inflated_sales": "直接关联后商品金额（元）", "inflated_payment": "直接关联后支付金额（元）",
    "order_sales": "整单商品金额（元）", "order_freight": "整单运费（元）", "difference": "支付差额（元）",
    "order_status": "订单状态", "purchase_at": "下单时间", "delivered_at": "送达时间", "estimated_delivery_at": "预计送达时间",
    "customer_unique_id": "自然客户ID", "customer_state": "客户省份", "review_score": "评价分数",
    "approved_at": "支付审批时间", "shipped_at": "发货时间",
    "order_item_id": "明细序号", "product_id": "商品ID", "product_name": "商品", "category_id": "品类ID",
    "category_name_zh": "品类", "seller_id": "商家ID", "seller_name": "商家", "price": "成交价（元）", "freight_value": "运费（元）",
    "payment_sequence": "支付序号", "payment_type": "支付方式", "payment_installments": "分期期数", "payment_value": "支付额（元）",
    "review_title": "评价标题", "review_message": "评价内容", "review_created_at": "评价时间", "issue_count": "问题记录数",
    "previous_sales": "上月销售额（元）", "previous_month": "上个月份", "mean_rate": "商家平均延迟率",
    "std_rate": "延迟率标准差", "z_score": "标准差倍数", "total_change": "整体净变化额（元）", "contribution_rate": "净增长贡献率",
    "current_orders": "本期订单数", "previous_orders": "基期订单数",
}

STATUS_LABELS = {"delivered": "已送达", "shipped": "已发货", "approved": "已确认", "processing": "处理中", "created": "已创建", "invoiced": "已开票", "canceled": "已取消", "unavailable": "缺货"}


def period_text(period):
    return f"{period.start} 至 {period.end - timedelta(days=1)}"


def metric_description(metric):
    text = METRICS[metric]
    for technical, readable in {"customer_unique_id": "同一自然客户", "order_payments": "支付记录", "order_id": "订单编号", "price": "成交价"}.items():
        text = text.replace(technical, readable)
    return text


def label(column: str) -> str:
    if column in LABELS:
        return LABELS[column]
    for suffix, name in (("_current", "本期"), ("_previous", "基期"), ("_change", "变化量"), ("_rate", "变化率")):
        if column.endswith(suffix) and column[:-len(suffix)] in LABELS:
            return LABELS[column[:-len(suffix)]] + "·" + name
    return column


def plan_label(column: str, plan: AnalysisPlan) -> str:
    text = label(column)
    return text.replace("已送达", "所选状态") if plan.order_statuses != ["delivered"] else text


def cell(column: str, value, plan: AnalysisPlan) -> str:
    if value is None:
        return "不可计算/无数据"
    if column == "order_status":
        return STATUS_LABELS.get(value, value)
    metric = plan.metric_ids[0] if column in {"current_value", "previous_value", "absolute_change"} and plan.metric_ids else column
    for suffix in ("_current", "_previous", "_change"):
        if metric.endswith(suffix):
            metric = metric[:-len(suffix)]
    if column in {"contribution_rate", "mean_rate", "std_rate"} or (column.endswith("_rate") and column not in {"delay_rate", "low_score_rate"}):
        metric = "change_rate"
    if metric in {"delay_rate", "low_score_rate", "change_rate"}:
        suffix = " 个百分点" if column == "absolute_change" or column.endswith("_change") else "%"
        return str((Decimal(str(value)) * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)) + suffix
    if metric in {"sales", "payment_amount", "avg_review", "inflated_sales", "inflated_payment", "order_sales", "order_freight", "difference", "price", "freight_value", "payment_value", "total_change"}:
        return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return str(value).replace("|", "\\|").replace("\n", " ")


def explain(plan: AnalysisPlan) -> str:
    if plan.explanation_topic == "joins":
        return "\n\n".join(RELATIONSHIPS[:2]) + "\n\n本解释引用表关系和指标口径，未执行数值查询。"
    if plan.explanation_topic == "causality":
        return "材料没有促销活动、投放或对照组数据。可以比较期间销售变化及品类、商家构成，但现有证据不足以把变化归因于促销。"
    return "\n\n".join(f"**{label(key)}**：{metric_description(key)}" for key in METRICS)


def conclusion(plan: AnalysisPlan, result: QueryResult) -> str:
    row = dict(zip(result.columns, result.rows[0]))
    if plan.intent == "join_example":
        return f"真实订单 {row['order_id']} 有 {row['item_rows']} 条商品明细、{row['payment_rows']} 条支付记录，直接关联会产生 {row['joined_rows']} 行。商品金额应为 {cell('sales', row['sales'], plan)} 元，直接关联求和会变成 {cell('inflated_sales', row['inflated_sales'], plan)} 元。应分别按订单汇总后再关联。"
    if plan.intent == "compare":
        rate = row["change_rate"]
        direction = "增长" if rate is not None and rate > 0 else "下降" if rate is not None and rate < 0 else "持平"
        change = f"{direction}，变化率 {cell('change_rate', rate, plan)}" if rate is not None else "变化率不可计算（基期为零或数据缺失）"
        return f"本期 {cell('current_value', row['current_value'], plan)}，基期 {cell('previous_value', row['previous_value'], plan)}；{change}。"
    if plan.intent == "rank" and row[plan.metric_ids[0]] is not None:
        ranked_rows = [dict(zip(result.columns, values)) for values in result.rows]
        leaders = [r for r in ranked_rows if r["rank_no"] == 1]
        names = "、".join(r["entity_name"] for r in leaders)
        metric = plan.metric_ids[0]
        return f"{'并列第一' if len(leaders) > 1 else '排名第一'}：{names}；{LABELS[metric]}为 {cell(metric, row[metric], plan)}。"
    if plan.dimension == "total":
        return "；".join(f"{LABELS[m]}：{cell(m, row[m], plan)}" for m in plan.metric_ids) + "。"
    dimension = {"month": "月份", "category": "品类", "seller": "商家", "delivery_status": "延迟/准时状态", "customer_state": "客户省份"}[plan.dimension]
    return f"已按{dimension}汇总，具体数值及样本量见下表。"


def answer(plan: AnalysisPlan, result: QueryResult, manifest: dict) -> str:
    from app.advanced import extended
    if extended(plan):
        return advanced_answer(plan, result, manifest)
    period = plan.time_range
    header = f"**{manifest['name']}** · 下单日期：{period_text(period)}"
    if result.status not in {"ok", "empty"}:
        return header + f"\n\n本次未完成统计：{result.error}"
    if result.status == "empty":
        return header + "\n\n没有符合条件的分组记录。"
    columns = result.columns
    table = "| " + " | ".join(LABELS[c] for c in columns) + " |\n"
    table += "| " + " | ".join("---" for _ in columns) + " |\n"
    table += "\n".join("| " + " | ".join(cell(c, v, plan) for c, v in zip(columns, row)) + " |" for row in result.rows)
    notes = [metric_description(m) for m in plan.metric_ids]
    if plan.intent == "join_example":
        notes = ["按订单ID选取期间内第一笔同时具有多条商品明细和多条支付记录的已送达订单，放大金额由实际行数和订单合计计算。"]
    if plan.comparison_range:
        notes.append(f"对比日期：{period_text(plan.comparison_range)}；基期为0时变化率不可计算。")
    if plan.filters:
        names = {"category_id": dict(manifest["entities"]["categories"]), "seller_id": dict(manifest["entities"]["sellers"])}
        notes.append("筛选条件：" + "；".join(f"{label(f.field)}：{'、'.join(names.get(f.field, {}).get(value, value) for value in f.values)}" for f in plan.filters))
    if plan.intent == "rank":
        notes.append(f"每组至少 {plan.min_group_count} 笔已送达订单，按首个指标取前 {plan.top_n} 名并保留边界并列。")
    if result.truncated:
        notes.append(f"当前显示第{result.row_offset+1}—{result.row_offset+len(result.rows)}条，后面还有记录，可翻页或导出全部结果。统计使用全部符合条件的数据。")
    return header + "\n\n" + conclusion(plan, result) + "\n\n" + table + "\n\n口径：" + "；".join(notes)


def advanced_answer(plan: AnalysisPlan, result: QueryResult, manifest: dict) -> str:
    text = f"**{manifest['name']}** · {label(plan.time_field)}：{period_text(plan.time_range)} · 状态：{'、'.join(STATUS_LABELS.get(s, s) for s in plan.order_statuses)}"
    if result.status not in {"ok", "empty"}:
        return text + f"\n\n未完成查询：{result.error}"
    if not result.rows:
        return text + "\n\n没有符合当前条件的记录。"
    notes = []
    if plan.intent == "contribution":
        first = dict(zip(result.columns, result.rows[0]))
        metric = plan.metric_ids[0]
        notes.append(f"增长贡献最大的分组：{first['entity_name']}，变化额 {cell(metric+'_change', first[metric+'_change'], plan)}。")
        notes.append("贡献率=分组变化额/整体净变化额；下降可为负，正向贡献可超过100%；净变化为0时不可计算。")
    elif plan.intent == "compare":
        notes.append("按分组对齐本期与基期；可加指标缺失分组记0，缺评分/比率保留不可计算；基期0时变化率不可计算。")
    elif plan.intent == "diagnose":
        threshold = plan.diagnostic_threshold
        descriptions = {"reconciliation": f"支付金额与整单商品金额+运费对账，阈值{threshold if threshold is not None else .01}元；未分摊给品类或商家。",
                        "quality": "逐项报告缺失、重复、负金额与异常日期，不自动修补来源数据。",
                        "sales_spike": f"标出相邻月份销售变化率绝对值≥{threshold if threshold is not None else .3}的记录；不跨缺月比较。",
                        "seller_outlier": f"标出延迟率高于商家平均值{threshold if threshold is not None else 2}个标准差的商家；均值不按订单量加权，订单门槛{plan.min_group_count}。"}
        notes.append(descriptions[plan.diagnosis])
        notes.append("这是数据偏离或对账线索，不能据此确认促销、欺诈等业务原因。")
    elif plan.intent == "details":
        notes.append("订单金额是完整订单合计；明细筛选用于选择订单或明细，不将整单支付分摊到商品。")
    if plan.comparison_range:
        notes.append(f"对比日期：{period_text(plan.comparison_range)}。")
        if plan.min_group_count > 1:
            notes.append(f"仅展示两期均至少{plan.min_group_count}单的分组；贡献率分母仍使用全部分组的净变化。")
    if plan.order_where or plan.item_where:
        notes.append("订单条件按整单金额、状态或评分筛选，商品条件按商品明细筛选。")
    table = "| " + " | ".join(plan_label(c, plan) for c in result.columns) + " |\n| " + " | ".join("---" for _ in result.columns) + " |\n"
    table += "\n".join("| " + " | ".join(cell(c, v, plan) for c, v in zip(result.columns, row)) + " |" for row in result.rows[:30])
    notes.append(f"本页共{len(result.rows)}条，聊天中最多展示30条；完整结果可在下方表格查看或导出。")
    return text + "\n\n" + "\n\n".join(notes) + "\n\n" + table
