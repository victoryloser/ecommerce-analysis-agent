"""Business prose assembled from completed evidence, without a model call."""
from app.answers import cell, label, period_text, STATUS_LABELS
from app.models import AnalysisPlan, QueryResult


LIMITATIONS = {
    "causality": "品类贡献描述变化的构成，不能证明促销因果；当前数据没有促销活动、投放或对照信息。",
    "profit": "无法计算促销带来的利润增量：缺少促销归属、成本、退款及相关费用，也没有因果识别依据。商品额和运费仍按现有数据计算。",
}
ISSUES = {"exact_duplicate": "完全重复商品明细", "price_conflict": "同键价格冲突", "missing_payment": "整单支付缺失", "other": "其他字段问题", "all": "所选问题合计（订单去重）"}


def scope_text(plan):
    def predicate(node):
        if node.children:
            text = (" 且 " if node.conjunction == 'all' else " 或 ").join(predicate(c) for c in node.children)
        else:
            operators = {'eq':'=', 'ne':'≠', 'gt':'>', 'ge':'≥', 'lt':'<', 'le':'≤',
                         'in':'属于', 'not_in':'不属于', 'between':'介于', 'is_null':'缺失', 'not_null':'非缺失'}
            text = label(node.field) + operators[node.operator] + '、'.join(str(v) for v in node.values)
        return ('非' if node.negate else '') + '(' + text + ')'
    parts = [label(plan.time_field) + '：' + period_text(plan.time_range)] if plan.time_range else []
    if plan.comparison_range:
        parts.append('比较期：' + period_text(plan.comparison_range))
    parts.append('状态：' + '、'.join(STATUS_LABELS.get(s, s) for s in plan.order_statuses))
    parts += [label(f.field) + '：' + '、'.join(f.values) for f in plan.filters]
    parts += [predicate(p) for p in (plan.order_where, plan.item_where) if p]
    if plan.selected_order_id:
        parts.append('订单：' + plan.selected_order_id)
    if not plan.filters and not plan.item_where:
        parts.append('无明细筛选')
    return '；'.join(parts)


def scope_view(evidence, session):
    from app.presentation import display_evidence
    record = display_evidence(evidence)
    if not record.get('plan'):
        return ''
    shown = AnalysisPlan.model_validate(record['plan'])
    success = (record.get('result') or {}).get('status') in {'ok','empty'}
    text = ('当前表格范围：' if success else '当前请求范围（尚未完成）：') + scope_text(shown)
    working = session.working_plan if session else None
    if working and working.intent == 'workflow':
        index = (session.focus_step or 1)-1
        working = working.steps[index]
    if working and scope_text(working) != scope_text(shown):
        text += '\n\n分析任务仍保留：' + scope_text(working)
    return text


def inspection_answer(plan, result, manifest):
    period = plan.comparison_range if plan.diagnosis in {"quality_summary", "quality_rows"} and plan.quality_scope == "previous" else plan.time_range
    header = f"**{manifest['name']}** · {period_text(period)}"
    if plan.comparison_range and plan.quality_scope == "comparison_union":
        header += f"；比较期 {period_text(plan.comparison_range)}（按区间并集查验）"
    if result.status not in {"ok", "empty"}:
        return header + "\n\n核查未完成：" + (result.error or result.status)
    rows = [dict(zip(result.columns, r)) for r in result.rows]
    if plan.diagnosis == "quality_summary":
        table = "| 问题 | 明细键/订单键数 | 涉及订单 | 待确认订单 | 重复多余行 |\n|---|---:|---:|---:|---:|\n"
        table += "\n".join(f"| {ISSUES[r['issue_type']]} | {r['issue_key_count']} | {r['affected_orders']} | {r['uncertain_orders']} | {r['extra_duplicate_rows']} |" for r in rows)
        return header + "\n\n" + table + "\n\n完全重复只保留一份有效明细；正式价格冲突订单整单暂不计入商品额和运费，订单数保留；支付缺失表示支付额未知，不等于客户未付款。待确认归属不计入已确认异常；正常多商品和拆分支付保留。"
    if plan.diagnosis == "quality_rows":
        if not rows:
            return header + "\n\n本次成功查验未发现所选类型的问题记录。"
        table = "| 问题 | 业务键 | 订单 | 范围归属 |\n|---|---|---|---|\n"
        table += "\n".join(f"| {ISSUES[r['issue_type']]} | {r['business_key']} | {r['order_id']} | {'已确认' if r['membership_confirmed'] else '待确认'} |" for r in rows[:30])
        return header + "\n\n" + table + "\n\n完整记录可翻页或导出。source_rows为含表头的逻辑记录序号，candidates为不同候选，二者不能逐项对应；具体来源记录可展开核查。"
    if not rows:
        return header + "\n\n没有找到满足确定归属和金额准入条件的核查候选；不代表没有关联订单。"
    row = rows[0]
    if plan.diagnosis == "order_candidate":
        return f"已从商家 {row['seller_id']} 确定匹配且金额准入的订单中，按订单ID选择 {row['order_id']} 进行核查。"
    fields = [("seller_sales", "选中商家商品小计"), ("order_sales", "整单商品额"),
              ("order_freight", "整单运费"), ("payment_amount", "整单支付额")]
    text = header + f"\n\n订单 **{row['order_id']}**，商家 **{row['selected_seller_id']}**：\n\n"
    text += "| 核查项 | 金额（元） |\n|---|---:|\n" + "\n".join(f"| {name} | {cell(key, row[key], plan)} |" for key, name in fields)
    text += f"\n\n支付记录 {row['payment_records']} 条，按订单独立汇总，不分摊到商家。商品明细展示整单全部记录，选中商家另作标记。"
    if row.get("amount_eligible") is False:
        text += "该订单因价格冲突暂不计入正式商品额和运费；以上为事实金额，已确认小计不改写为0。"
    return text


def comparison_answer(plan, result, manifest):
    row = dict(zip(result.columns, result.rows[0]))
    ratios = result.ratios[0] if result.ratios else {}
    text = f"**{manifest['name']}** · {label(plan.time_field)} · 状态：{'、'.join(STATUS_LABELS.get(s, s) for s in plan.order_statuses)}\n\n"
    text += "| 期间 | " + " | ".join(label(m) for m in plan.metric_ids) + " |\n|---|" + "---:|" * len(plan.metric_ids) + "\n"
    for period, suffix in [(plan.comparison_range, "previous"), (plan.time_range, "current")]:
        text += "| " + period_text(period) + " | " + " | ".join(cell(m, row[m + '_' + suffix], plan) for m in plan.metric_ids) + " |\n"
    if "sales" in plan.metric_ids:
        text += f"\n商品额差额 {cell('sales_change', row['sales_change'], plan)} 元，环比 {cell('sales_rate', row['sales_rate'], plan, ratios)}。"
    return text + "\n\n按两个期间分别统计去重订单，商品额及运费汇总匹配明细；差额和环比使用未舍入金额计算。完整计算列与SQL可在依据中查看。"


def workflow_answer(plan, tasks, manifest):
    if plan.output_mode == "customer_summary" and all(t['status'] == 'completed' for t in tasks):
        values = [t['evidence']['result']['rows'][0][0] for t in tasks]
        return (f"**{manifest['name']}** · {period_text(plan.time_range)} · 状态：{'、'.join(STATUS_LABELS.get(s, s) for s in plan.order_statuses)}\n\n"
                f"订单总数：{values[0] if values[0] is not None else '尚不能确认'} 笔；复购自然客户数：{values[1] if values[1] is not None else '尚不能确认'} 人。\n\n"
                "订单按order_id去重；通过customer_unique_id识别自然客户，至少有两笔不同的所选状态订单计为复购。两项统计使用同一期间及筛选。")
    parts = []
    for i, task in enumerate(tasks, 1):
        state = task["status"]
        if state in {"completed", "unsupported"}:
            parts.append(task.get("text", ""))
        else:
            name = {"needs_input": "需要补充", "failed": "尚未完成", "pending": "等待执行"}.get(state, state)
            parts.append(f"步骤{i} · {name}：{task.get('reason') or task.get('text') or '等待依赖结果'}")
    parts.extend(LIMITATIONS[k] for k in dict.fromkeys(plan.limitations))
    return "\n\n".join(dict.fromkeys(paragraph for p in parts for paragraph in p.split("\n\n") if paragraph))


def audit_items_answer(plan, result):
    fields = ['order_item_id','product_name','seller_id','price','freight_value','belongs_to_selected_seller']
    rows = [dict(zip(result.columns,r)) for r in result.rows]
    text = f"订单 {plan.selected_order_id} 的商品明细（包含所有商家）：\n\n"
    text += '| ' + ' | '.join(label(f) for f in fields) + ' |\n|' + '---|' * len(fields) + '\n'
    text += '\n'.join('| ' + ' | '.join(cell(f,r[f],plan) for f in fields) + ' |' for r in rows[:30])
    if result.truncated or len(rows)>30:
        text += '\n\n其余明细可在结果表翻页或完整导出。'
    return text
