import hashlib
import json
from pathlib import Path
from typing import get_args

from app.config import DATA_DIR, METADATA_VERSION
from app.models import AnalysisIntent, AnalysisPlan
from app.metadata import METRIC_DETAILS, metric_detail, required_tables, table_detail
from app.source import SCHEMA
from app.grouping import dimensions, JOINT_GROUPS
from app.calculation import CalculationContext, LEGACY_CONTEXT

METRICS = {
    "freight": "有效商品明细 freight_value 合计，元；保留明细归属及绑定快照的金额规则",
    "sales": "已送达订单商品明细 price 合计，元，不含运费；按品类/商家保留明细归属",
    "order_count": "已送达订单数；分组内 order_id 去重",
    "delay_rate": "延迟的已送达订单数/已送达订单数，0~1；任一送达/预计时间缺失则该组比例不可计算",
    "avg_review": "有评价的已送达订单平均分；无评价不记零分",
    "low_score_rate": "评分≤2的已送达订单数/有评价的已送达订单数，0~1",
    "repeat_customers": "统计期内 customer_unique_id 至少2笔已送达订单的自然客户数",
    "payment_amount": "已送达订单支付额：order_payments 先按订单汇总；不能称为商品销售额",
}
RELATIONSHIPS = [
    "orders 1:N order_items，orders 1:N order_payments；两张明细直接关联会产生 N×M 行并重复金额。",
    "订单金额分析须先分别按订单汇总商品和支付再关联；品类/商家销售额须保留明细归属。",
    "orders N:1 customers；order_items N:1 products / sellers；products N:1 categories。",
    "商家订单数按订单—商家去重；各商家订单数相加不能作为全站订单数。",
]


def metadata_hash(context=LEGACY_CONTEXT) -> str:
    content = {"version": METADATA_VERSION, "schema": {t: table_detail(t) for t in SCHEMA},
               "metrics": {m: metric_detail(m, context) for m in METRICS}, "relationships": RELATIONSHIPS}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PlanError(ValueError):
    """不受支持或需要用户澄清的业务计划。"""


def load_manifest(dataset_id: str, data_dir: Path = DATA_DIR) -> dict:
    return json.loads((data_dir / f"{dataset_id}.manifest.json").read_text(encoding="utf-8"))


def catalog(manifest: dict) -> dict:
    context = CalculationContext.from_manifest(manifest)
    return {
        "quality_rule_version": context.rule_version, "effective_metadata_sha256": metadata_hash(context),
        "dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
        "coverage": manifest["coverage"], "metrics": {m: {"summary": description, **metric_detail(m, context)} for m, description in METRICS.items()},
        "coverage_fields": manifest.get("coverage_fields", {"purchase_at": manifest["coverage"]}),
        "dimensions": ["total", "month", "category", "seller", "delivery_status", "customer_state"],
        "joint_groups": JOINT_GROUPS,
        "entities": manifest["entities"], "relationships": RELATIONSHIPS,
        "time_field": "默认purchase_at；可显式选择approved_at/shipped_at/delivered_at/estimated_delivery_at，均为左闭右开",
        "order_statuses": manifest["entities"].get("order_statuses", ["delivered", "canceled", "shipped", "processing", "approved", "created", "invoiced", "unavailable"]),
        "supported_combinations": {
            "repeat_customers": "仅此指标，total/month/customer_state 维度",
            "payment_amount": "禁止 category/seller 维度和 category_id/seller_id 过滤；无支付额分摊规则",
            "compare": "支持多个指标及total/category/seller/customer_state/delivery_status分组，显式comparison_range",
            "contribution": "单一可加金额sales/freight/payment_amount，非total/month维度，按变化额排名并计算对净增长的贡献率；不分摊支付",
            "details": "orders/items/payments/reviews明细；可按selected_order_id选择订单",
            "diagnose": "reconciliation支付与商品加运费对账、quality质量、sales_spike月销售突变、seller_outlier商家延迟率偏离",
            "workflow": "最多6个步骤；可独立执行、澄清、标明不支持；逐步保存及续算，不嵌套",
            "history": "检索已保存会话，history_query为内容，history_start/end按会话更新时间左闭右开筛选",
            "predicates": "order_where用于订单粒度（order_sales/order_freight为完整订单金额），item_where用于明细粒度；分别支持all/any/negate与比较/IN/NOT IN/BETWEEN/空值",
            "rank": "非total维度，按第一个指标降序；按订单数门槛过滤，并保留边界并列",
            "joint_analysis": "group_by支持category+seller和month+category；商品额、运费、订单、延迟和整单评价；禁止支付分摊与联合复购。含month仅汇总/排名。rank_within指定组内排名，contribution_within指定品类内分母；contribution_direction=decrease查看下降驱动。",
        },
    }


def get_catalog(manifest: dict, plan: AnalysisPlan) -> dict:
    context = CalculationContext.from_manifest(manifest)
    def fields(node):
        return ([node.field] if node.field else []) + [field for child in node.children for field in fields(child)] if node else []
    used = [f.field for f in plan.filters] + fields(plan.order_where) + fields(plan.item_where)
    tables = required_tables(plan.metric_ids, dimensions(plan), used, plan.intent)
    for step in plan.steps:
        tables.update(get_catalog(manifest, step)["tables"])
    if plan.explanation_topic == "joins":
        tables.update({"orders", "order_items", "order_payments"})
    if plan.intent in {"details", "diagnose"}:
        tables.update(SCHEMA)
    result = {"dataset_id": manifest["dataset_id"], "metadata_version": manifest["metadata_version"],
            "metadata_sha256": manifest["metadata_sha256"],
            "quality_rule_version": context.rule_version, "effective_metadata_sha256": metadata_hash(context),
            "metrics": {m: metric_detail(m, context) for m in plan.metric_ids},
            "tables": {t: table_detail(t) for t in sorted(tables)}, "relationships": RELATIONSHIPS}
    result["applied_scope"] = {"time_field": plan.time_field, "order_statuses": plan.order_statuses,
                               "group_by": list(dimensions(plan)), "grouping_version": "joint-v1",
                               "rank_within": plan.rank_within, "contribution_within": plan.contribution_within,
                               "order_where": plan.order_where.model_dump(mode="json") if plan.order_where else None,
                               "item_where": plan.item_where.model_dump(mode="json") if plan.item_where else None}
    for metric in result["metrics"].values():
        metric["required_filter"] = {"orders.order_status": plan.order_statuses}
        metric["time_field"] = "orders." + plan.time_field
    if "orders" in result["tables"]:
        result["tables"]["orders"]["fields"]["order_status"]["meaning"] = "本次查询选定状态：" + ",".join(plan.order_statuses)
    return result


def validate_workflow(plan: AnalysisPlan, manifest: dict) -> None:
    if not plan.steps:
        raise PlanError("多步骤分析必须提供steps")
    for index, step in enumerate(plan.steps, 1):
        intent = step.pending_intent if step.intent == "clarify" else step.intent
        if intent not in {*get_args(AnalysisIntent), "explain", "unsupported"}:
            raise PlanError("工作流步骤必须是分析、解释或保留分析意图的澄清，不嵌套工作流")
        if step.source_step and (step.source_step >= index or (step.bind_mode == "field" and not step.bind_field)):
            raise PlanError("依赖步骤必须引用前面的步骤并声明bind_field")
        validate_plan(step, manifest, allow_binding=True)


def validate_plan(plan: AnalysisPlan, manifest: dict, *, allow_binding=False) -> None:
    CalculationContext.from_manifest(manifest)
    if plan.dataset_id != manifest["dataset_id"] or plan.metadata_version != manifest["metadata_version"]:
        raise PlanError("计划的数据集或元数据版本与当前会话不一致")
    if plan.source_step and not allow_binding:
        raise PlanError("步骤依赖只用于workflow，执行前必须由宿主绑定真实结果")
    if plan.binding_updates:
        if not (allow_binding and plan.source_step):
            raise PlanError("绑定后的覆盖仅用于有来源依赖的工作流步骤")
        from app.plan_update import apply_updates
        candidate = apply_updates(plan.model_copy(update={"binding_updates": []}), plan.binding_updates, manifest)
        return validate_plan(candidate, manifest, allow_binding=allow_binding)
    if plan.audit_seller_id and plan.audit_seller_id not in {r[0] for r in manifest["entities"]["sellers"]}:
        raise PlanError("核查商家不在当前数据目录中")
    dims = dimensions(plan)
    if plan.group_by and dims not in JOINT_GROUPS:
        raise PlanError("联合分组仅支持品类与商家、月份与品类")
    if plan.group_by and plan.intent not in {"aggregate", "rank", "compare", "contribution", "clarify"}:
        raise PlanError("联合分组用于汇总、排名、期间比较与商品额贡献")
    if plan.rank_within and (len(dims) != 2 or plan.rank_within not in dims or plan.intent not in {"rank", "clarify"}):
        raise PlanError("组内排名需要联合维度及其中一个排名分区")
    if plan.contribution_within and (plan.intent != "contribution" or dims != ("category", "seller") or plan.contribution_within != "category"):
        raise PlanError("父组贡献仅支持品类与商家分析中的品类内贡献")
    if plan.bind_mode == "group" and not plan.source_step:
        raise PlanError("组合绑定需要来源步骤")
    is_workflow = plan.intent == "workflow" or (plan.intent == "clarify" and plan.pending_intent == "workflow")
    if plan.steps and not is_workflow:
        raise PlanError("仅workflow使用steps")
    if plan.intent in {"clarify", "unsupported"}:
        if not plan.clarification:
            raise PlanError("澄清/不支持计划必须说明具体原因")
        if plan.pending_intent == "workflow":
            validate_workflow(plan, manifest)
            if not any(step.intent == "clarify" for step in plan.steps):
                raise PlanError("待澄清工作流必须保留需要澄清的步骤")
            return
        if "year" in plan.missing_slots and (not plan.pending_intent or not plan.month):
            raise PlanError("年份澄清必须保留分析意图和月份")
        if "year" in plan.missing_slots and plan.pending_intent in {"aggregate", "rank", "compare", "contribution"} and not plan.metric_ids:
            raise PlanError("指标分析的年份澄清必须保留指标")
        if "year" in plan.missing_slots and plan.pending_intent in {"compare", "contribution"} and not (plan.comparison_month or plan.comparison_range):
            raise PlanError("比较问题的年份澄清必须保留基期月份")
        if plan.entity_candidates:
            from app.clarification import entity_values
            allowed = {field: {key for key, _ in records} for field, records in entity_values(manifest).items()}
            if any(c.value not in allowed[c.field] for c in plan.entity_candidates):
                raise PlanError("澄清选项包含字典外实体")
        return
    def has_review(node):
        return bool(node and (node.field == "review_score" or any(has_review(child) for child in node.children)))
    if has_review(plan.order_where) and manifest["quality"]["duplicate_review_orders"]:
        raise PlanError("一单多评价时评分筛选需先明确归并口径")
    if plan.intent == "load_catalog":
        return
    if plan.intent == "explain":
        if plan.explanation_topic is None:
            raise PlanError("解释问题必须选择元数据主题")
        return
    if plan.intent == "history":
        if plan.history_start and plan.history_end and plan.history_start >= plan.history_end:
            raise PlanError("历史检索起始日期必须早于截止日期")
        return
    if plan.intent == "workflow":
        validate_workflow(plan, manifest)
        return
    if plan.time_range is None:
        raise PlanError("缺少统计时间范围，请补充年份和期间")
    if plan.intent == "join_example":
        if plan.metric_ids or plan.filters or plan.dimension != "total" or plan.time_field != "purchase_at" or plan.order_statuses != ["delivered"] or plan.order_where or plan.item_where:
            raise PlanError("真实关联示例只支持指定期间的订单示例，不附加指标或实体过滤")
        return
    from app.predicates import predicate_sql, ORDER_FIELDS, ITEM_FIELDS
    predicate_sql(plan.order_where, ORDER_FIELDS)
    predicate_sql(plan.item_where, ITEM_FIELDS)
    known_statuses = set(manifest["entities"].get("order_statuses", [])) | {"delivered"}
    if not set(plan.order_statuses) <= known_statuses:
        raise PlanError("订单状态不在当前数据目录中")
    if plan.intent in {"details", "diagnose"}:
        quality_check = plan.intent == "diagnose" and plan.diagnosis in {"quality_summary", "quality_rows"}
        if quality_check and plan.quality_scope == "previous" and plan.comparison_range is None:
            raise PlanError("当前分析没有比较基期")
        if plan.comparison_range is not None and not quality_check:
            raise PlanError("明细/诊断不使用comparison_range")
        if plan.intent == "diagnose" and plan.diagnosis == "order_candidate" and not plan.source_step:
            sellers = [f for f in plan.filters if f.field == "seller_id"]
            if len(sellers) != 1 or len(sellers[0].values) != 1:
                raise PlanError("选单需要唯一已确认的商家")
        if plan.intent == "diagnose" and plan.diagnosis == "order_audit" and not plan.source_step:
            if not plan.selected_order_id or not plan.audit_seller_id:
                raise PlanError("整单核查需要订单与商家")
        if plan.intent == "diagnose" and plan.diagnosis == "seller_outlier" and plan.item_where is not None:
            raise PlanError("商家异常诊断暂不与明细条件混用，请使用商家/品类实体筛选")
        for filter_ in plan.filters:
            source = {"category_id": [r[0] for r in manifest["entities"]["categories"]], "seller_id": [r[0] for r in manifest["entities"]["sellers"]], "customer_state": manifest["entities"]["customer_states"]}[filter_.field]
            if not set(filter_.values) <= set(source):
                raise PlanError("明细/诊断包含字典外的实体")
        return
    if not plan.metric_ids or len(set(plan.metric_ids)) != len(plan.metric_ids):
        raise PlanError("需要不重复的指标列表")
    if plan.intent == "rank" and not dims:
        raise PlanError("排名需要分组维度")
    if not dims and plan.min_group_count != 1:
        raise PlanError("订单数门槛只用于分组分析")
    if plan.intent in {"compare", "contribution"}:
        if plan.comparison_range is None or "month" in dims:
            raise PlanError("比较需要明确基期；逐月序列请按月汇总，分组比较选择品类/商家/省份/履约状态")
        if plan.intent == "contribution" and (len(plan.metric_ids) != 1 or plan.metric_ids[0] not in {"sales", "freight", "payment_amount"} or not dims):
            raise PlanError("增长贡献需要一个可加金额指标及非total分组，不能把比例或重复归属订单数直接相加")
    elif plan.comparison_range is not None:
        raise PlanError("仅compare计划使用comparison_range")
    fields = [f.field for f in plan.filters]
    if len(fields) != len(set(fields)):
        raise PlanError("同一字段请合并成一个过滤器")
    allowed = {
        "category_id": {r[0] for r in manifest["entities"]["categories"]},
        "seller_id": {r[0] for r in manifest["entities"]["sellers"]},
        "customer_state": set(manifest["entities"]["customer_states"]),
    }
    for filter_ in plan.filters:
        if not set(filter_.values) <= allowed[filter_.field]:
            raise PlanError(f"{filter_.field} 包含字典外的值，请选择已提供的实体ID")
    if "repeat_customers" in plan.metric_ids:
        if len(plan.metric_ids) != 1 or plan.group_by or plan.dimension not in {"total", "month", "customer_state"}:
            raise PlanError("复购客户数仅支持独立计算及全站/月/客户省份分组")
        if plan.min_group_count != 1:
            raise PlanError("复购分析暂不支持分组订单门槛")
    if "payment_amount" in plan.metric_ids:
        if set(dims) & {"category", "seller"} or set(fields) & {"category_id", "seller_id"} or plan.item_where is not None:
            raise PlanError("没有商家/品类支付额分摊规则，请改查商品销售额或全站支付额")
    if set(plan.metric_ids) & {"avg_review", "low_score_rate"} and manifest["quality"]["duplicate_review_orders"]:
        raise PlanError("存在一单多评价，需先明确评价归并口径")
