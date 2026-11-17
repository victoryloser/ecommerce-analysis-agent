import hashlib
import json
from datetime import datetime

from pydantic import ValidationError

from app.models import AnalysisPlan
from app.plan_update import apply_update, expand_periods
from app.clarification import bind_entities, resolve_slots
from app.providers import Provider
from app.semantic import PlanError, catalog, get_catalog, validate_plan
from app.session import Session
from app.trace import TurnTrace

INSTRUCTIONS = """你是经营分析计划器。根据真实用户问题输出一个符合给定JSON Schema的对象，禁止输出SQL或计算金额。
首轮目录已提供完整指标口径。通常直接输出计划；必要时可用load_catalog补充一次字段详情，禁止重复请求。
只使用目录里的指标、维度、实体ID，数据集和版本必须等于当前绑定值。所有计算固定以purchase_at筛选已送达订单。
默认purchase_at及delivered；用户显式指定其他日期或状态时填写time_field/order_statuses，必须使用目录中的状态，不得悄悄改回默认。
时间范围为左闭右开，end是最后一天的下一天；‘全期/共有/有多少’没有月份限定时使用所选time_field的coverage_fields覆盖范围，不要把下单时间覆盖范围套在送达等其他日期上。
若问题指定月份却没有年份，且没有可继承的年份、示例上下文也未启用，intent=clarify，具体询问年份，不猜测。
缺年份时必须保留已明确的metric_ids、dimension、group_by、rank_within、filters，设置missing_slots=["year"]、pending_intent为补全后的分析意图、month为当前月份；环比还设置comparison_month。用户补年份后宿主会按槽位直接补全，不得丢掉原条件。
pending_intent支持aggregate/rank/compare/contribution/details/diagnose/join_example；details、diagnose和join_example不要求metric_ids。贡献和比较均须保留基期月份或已经明确的comparison_range，1月对12月由宿主跨年补全。
同名实体无法唯一确定时用clarify并列出entity_candidates，每项包含field、value(真实ID)、label；过滤值也可使用字典中的完整名称，由宿主绑定到ID。
启用示例上下文时Q03~Q05的未写年份月份为2025年；仍按用户写出的订单门槛，禁止凭记忆套用另一数据集。
修改/删除条件时输出intent=update和update对象，operation=set/remove/reset，field为目标字段，values为替换值，金额条件可提供predicate；不要重写整个计划。宿主基于working_plan（失败后仍保留最新意图）更新。工作流用target_step或当前focus_step。OR/NOT组无法明确修改时只澄清该组。继续任务用intent=continue。
全期填写period_mode=full_coverage；明确日期用explicit_range。环比填写comparison_mode=previous_month；固定基期用fixed，删除比较用none。time_range更新通过update.period和period_mode传入；comparison_range通过update.period和comparison_mode传入。
用户明确提出新问题时放弃旧澄清，不继承无关的过滤条件。
compare需要显式当前和基期范围，支持多个指标及品类/商家/省份/履约状态分组。rank按首个指标降序且保留并列。
联合分析用group_by=[category,seller]或[month,category]且dimension=total；同一明细组成组合，不把订单的品类集合与商家集合交叉。支持sales/order_count/delay_rate/avg_review/low_score_rate；评价与履约属于含该组合的整单样本，不是商品独有评分。含month只做aggregate/rank，不做两期compare/contribution。
全局组合排名rank_within=null；每品类内商家排名rank_within=category，每月内品类排名rank_within=month。贡献可用contribution_within=category另列品类内分母，同时保留全范围贡献；看下降驱动用contribution_direction=decrease。Top N保留并列，分母不受Top N影响。
增加/去掉分组用update.field=group_by、values为完整的新分组列表；取消品类或商家筛选用category_id/seller_id，不能混淆。联合分组不支持支付分摊及复购，遇混合问题拆为可支持的独立步骤。
增长贡献用contribution、一个可加金额指标、品类/商家/省份/履约状态分组，输出变化额及占净增长比例；无支付分摊规则。
查询订单/商品明细/支付记录/评价详情用details及detail_level=orders/items/payments/reviews；指定订单填写selected_order_id。
异常诊断用diagnose及diagnosis=reconciliation/quality/sales_spike/seller_outlier；阈值diagnostic_threshold分别是元、未使用、变化率(默认0.3)、标准差倍数(默认2)。
订单金额/评分/状态的复杂筛选填写order_where；商品单价/商品ID/品类/商家的复杂筛选填写item_where。叶子指定field、operator、values；组合填写children及conjunction=all/any，可negate。不要在同一节点同时填写field和children。
复合问题用workflow，steps最多6个完整计划，每步dataset_id和metadata_version均相同；不嵌套，不丢弃后半句。
工作流某步缺信息时该步用clarify并保留pending_intent及条件；无法支持的子任务用unsupported和具体理由。宿主先执行独立就绪步骤，每步保存，补充后只继续未完成步骤。
后续步骤需要使用前面查到的最高品类/商家/订单时，source_step填来源步骤编号(从1开始)，source_row默认0，bind_field填category_id/seller_id/customer_state/order_id；宿主会把真实结果ID绑定为过滤，禁止在还未查询时猜测最高者ID。
需要绑定前面查到的完整组合时用bind_mode=group、source_step，bind_field留空；会绑定该行所有维度及月份。组内排名产生多个父组第一名时，用source_partition指定父组ID（月份则YYYY-MM）；source_row在所选分区内从0编号。未指定父组时不得随意用首行代表唯一第一名。补充分区可用update.field=source_partition。
找以前/上周/其他会话的结果用history，history_query为归一化指标或主题关键词，可用history_start/end限制更新时间；不要把历史检索误作重新查询原始数据。
‘谁最高/哪个最多/冠军/第一名’都表示rank且top_n=1，不能套用默认10；只有用户明确要求前N名时使用N。
‘有评价的延迟和准时订单平均分’用avg_review及delivery_status分组，全期；复购用repeat_customers，不能用customer_id。
‘销售额’指sales不含运费；‘销售收入’等含糊口径应澄清是商品销售额还是支付金额。
解释商品明细和支付重复用explain/joins；指标口径用explain/metrics；促销归因用explain/causality。
若用户明确要求用真实订单/实际数字演示明细与支付的关联放大，使用join_example，metric_ids=[]、dimension=total、明确time_range（未指定期间用全期），不附加实体过滤。仅问原理仍用explain而不查库。
缺信息输出clarify与clarification；不支持的分析输出unsupported与理由。纯解释不要求时间。
metadata中没有促销等数据，不能臆造字段或作因果结论。不要输出没有根据的数字。
"""


def make_plan(provider: Provider, manifest: dict, session: Session, question: str, practice_context: bool, trace: TurnTrace) -> tuple[AnalysisPlan, int]:
    if question.strip() in {"/continue", "继续", "继续未完成任务", "重试"} and session.working_plan:
        return session.working_plan.model_copy(deep=True), 0
    completed = resolve_slots(session.pending_plan, question)
    if completed is not None:
        completed = bind_entities(completed, manifest, question)
        completed = expand_periods(completed, manifest)
        validate_plan(completed, manifest)
        trace.record("slots_resolved", plan=completed.model_dump(mode="json"))
        return completed, 0
    context = {
        "catalog": catalog(manifest), "practice_year": manifest["practice"]["year_context"] if practice_context else None,
        "today": datetime.now().date().isoformat(),
        **session.summary(),
        "recent_messages": [m for m in session.history if m["role"] == "user"][-4:], "question": question,
    }
    messages = [
        {"role": "system", "content": INSTRUCTIONS + "\nJSON Schema:\n" + json.dumps(AnalysisPlan.model_json_schema(), ensure_ascii=False)},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
    ]
    # 仅修正输出结构；语义限制失败直接返回，不让模型绕过限制。
    repairs = 0
    while True:
        serialized = json.dumps(messages, ensure_ascii=False)
        while len(serialized.encode()) > 48000 and context["recent_messages"]:
            context["recent_messages"].pop(0)
            messages[1]["content"] = json.dumps(context, ensure_ascii=False, default=str)
            serialized = json.dumps(messages, ensure_ascii=False)
        if len(serialized.encode()) > 48000:
            raise PlanError("当前问题与所需元数据超过本轮上下文预算，请缩小分析范围")
        trace.consume("model")
        trace.record("planning_requested", context_bytes=len(serialized.encode()), request_sha256=hashlib.sha256(serialized.encode()).hexdigest())
        raw = provider.complete(messages)
        trace.record("planning_response", response=raw)
        try:
            plan = AnalysisPlan.model_validate_json(raw)
        except ValidationError as error:
            trace.record("plan_format_error", errors=error.errors(include_input=False, include_url=False))
            if repairs == 2:
                raise PlanError("模型连续三次未生成合法计划结构") from error
            repairs += 1
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": f"按同一schema修正JSON结构，不改变业务范围：{error}"},
            ])
            continue
        if plan.intent == "load_catalog":
            validate_plan(plan, manifest)
            trace.consume("catalog")
            details = get_catalog(manifest, plan)
            trace.record("catalog_loaded", metric_ids=plan.metric_ids, tables=list(details["tables"]))
            messages.extend([{"role": "assistant", "content": raw},
                             {"role": "user", "content": "所选目录详情如下。请据此输出最终分析/澄清计划：" + json.dumps(details, ensure_ascii=False)}])
            continue
        if plan.intent == "continue":
            if session.working_plan is None:
                raise PlanError("当前没有可继续的分析")
            plan = session.working_plan.model_copy(deep=True)
        elif plan.intent == "update":
            base = session.working_plan or session.last_successful_plan
            if base is None or plan.update is None:
                raise PlanError("请先指定分析，再修改条件")
            change = plan.update
            if base.intent == "workflow" and change.target_step is None:
                change = change.model_copy(update={"target_step": session.focus_step})
            plan = apply_update(base, change, manifest, session.workflow_tasks)
        plan = expand_periods(plan, manifest)
        plan = bind_entities(plan, manifest, question)
        validate_plan(plan, manifest)
        trace.record("plan_validated", plan=plan.model_dump(mode="json"))
        return plan, trace.model_calls
