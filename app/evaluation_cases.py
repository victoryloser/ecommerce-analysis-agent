"""验收意图预先定义，期望值不由被测模型生成。"""
from dataclasses import dataclass

from app.demo import practice_plans
from app.models import AnalysisPlan


@dataclass(frozen=True)
class EvaluationCase:
    case_id: str
    category: str
    question: str
    expected_plan: AnalysisPlan | None
    practice: bool = True
    conversation: str | None = None
    expected_status: str = "ok"


def cases_for(manifest: dict, suite: str) -> list[EvaluationCase]:
    plans = practice_plans(manifest)
    period = f"{manifest['coverage']['start']}（含）至{manifest['coverage']['end']}（不含）"
    minimum = manifest["practice"]["min_seller_orders"]

    def plan(**changes):
        return AnalysisPlan.model_validate(plans["Q02"].model_dump(mode="json") | changes)

    original = [EvaluationCase(r["question_id"], "original", r["question"], plans[r["question_id"]]) for r in manifest["practice"]["questions"]]
    rewrites = {
        "Q01": [f"{period}，状态已送达的订单去重后是多少笔？", f"帮我数一下{period}已经送达的订单，不是商品明细条数。"],
        "Q02": [f"请算{period}已送达订单的商品价款总额，运费别算进去。", f"{period}卖出的商品合计多少钱？只统计已送达，排除运费。"],
        "Q03": ["2025年四月的商品销售额相较三月，环比百分之多少？", "比较2025年3月和4月已送达商品销售额，以3月为基期告诉我4月涨跌比例。"],
        "Q04": ["2025年4月哪个商品品类的销售额排第一？给出品类和金额。", "只看2025年4月已送达商品，销售额冠军品类是谁，卖了多少钱？"],
        "Q05": [f"2025年5月，已送达订单至少{minimum}笔的商家里，谁的延迟比例最高？", f"给2025年5月的商家按延迟率降序，剔除已送达订单不足{minimum}笔的商家，只要第一名及并列。"],
        "Q06": [f"{period}，把有评分的已送达订单分为延迟和准时两组，各组平均评分多少？", f"比较{period}已送达订单中晚到和准点两组的平均评价，未评价的不要记零分。"],
        "Q07": [f"{period}，完成至少两笔已送达订单的独立自然客户一共有多少？", f"{period}有多少回头客？按customer_unique_id识别，并要求至少2笔已送达订单。"],
        "Q08": ["商品明细与支付流水都用order_id连接后再求和，为何数额变大？怎么处理？", "一个订单多件商品又分多次支付，为什么直接join会重复算金额？"],
    }
    extended = [EvaluationCase(f"{qid}-R{index}", "rewrite", question, plans[qid]) for qid, questions in rewrites.items() for index, question in enumerate(questions, 1)]
    april = {"start": "2025-04-01", "end": "2025-05-01"}
    march = {"start": "2025-03-01", "end": "2025-04-01"}
    february = {"start": "2025-02-01", "end": "2025-03-01"}
    june = {"start": "2025-06-01", "end": "2025-07-01"}
    half = {"start": "2025-01-01", "end": "2025-07-01"}
    variants = [
        ("V01", "2025年3月已送达订单有多少笔？", plan(metric_ids=["order_count"], time_range=march)),
        ("V02", "2025年第一季度已送达商品销售额，不含运费，合计多少？", plan(time_range={"start": "2025-01-01", "end": "2025-04-01"})),
        ("V03", f"2025年6月已送达不少于{minimum}笔的商家中，延迟率前三名，保留并列。", plan(intent="rank", metric_ids=["delay_rate"], dimension="seller", time_range=june, min_group_count=minimum, top_n=3)),
        ("V04", "2025年6月按商品销售额排前三的品类有哪些？保留并列。", plan(intent="rank", dimension="category", time_range=june, top_n=3)),
        ("V05", "2025年上半年已送达订单的支付金额合计多少？同一订单的多次支付先汇总。", plan(metric_ids=["payment_amount"], time_range=half)),
        ("V06", f"{period}，已送达且有评价的订单按延迟和准时分组，分别求评分不超过2分的比例。", plan(metric_ids=["low_score_rate"], dimension="delivery_status")),
        ("V07", "2025年上半年完成至少两笔已送达订单的自然客户有多少？", plan(metric_ids=["repeat_customers"], time_range=half)),
        ("V08", "2025年4月电子家电品类的商品销售额合计多少？", plan(time_range=april, filters=[{"field": "category_id", "values": ["electronics"]}])),
        ("V09", "2025年上半年按月列出商品销售额和已送达订单数，不排名。", plan(metric_ids=["sales", "order_count"], dimension="month", time_range=half)),
        ("V10", f"2025年5月把商家门槛改为至少{minimum * 2}笔已送达订单，按延迟率列出前两名，保留并列。", plan(intent="rank", metric_ids=["delay_rate"], dimension="seller", time_range={"start": "2025-05-01", "end": "2025-06-01"}, min_group_count=minimum * 2, top_n=2)),
        ("V11", "用一笔真实的已送达订单和实际金额，展示商品明细与多次支付直接关联为什么会放大金额，使用材料全期。", plan(intent="join_example", metric_ids=[])),
    ]
    extended.extend(EvaluationCase(cid, "parameter", question, expected) for cid, question, expected in variants)
    multi = [
        ("M01", "4月商品销售额多少？", plan(intent="clarify", time_range=None, clarification="请补充年份", missing_slots=["year"], pending_intent="aggregate", month=4)),
        ("M02", "2025年", plan(time_range=april)),
        ("M03", "改成3月", plan(time_range=march)),
        ("M04", "跟2月相比呢？以2月为基期。", plan(intent="compare", time_range=march, comparison_range=february)),
        ("M05", "按品类列出2025年3月商品销售额，不做环比也不排名。", plan(dimension="category", time_range=march)),
        ("M06", "还是2025年3月，改查电子家电商品销售额合计。", plan(time_range=march, filters=[{"field": "category_id", "values": ["electronics"]}])),
        ("M07", "取消品类筛选，按月列出2025年上半年商品销售额。", plan(dimension="month", time_range=half)),
        ("M08", "重新开始", None),
        ("M09", "4月商品销售额呢？", plan(intent="clarify", time_range=None, clarification="请补充年份", missing_slots=["year"], pending_intent="aggregate", month=4)),
    ]
    extended.extend(EvaluationCase(cid, "multiturn", question, expected, practice=False, conversation="conditions") for cid, question, expected in multi)
    extended += [
        EvaluationCase("B01", "boundary", "2030年商品销售额多少？", plan(time_range={"start": "2030-01-01", "end": "2031-01-01"}), expected_status="out_of_coverage"),
        EvaluationCase("B02", "boundary", "促销是不是导致销售额增长的原因？", plan(intent="explain", time_range=None, explanation_topic="causality")),
        EvaluationCase("B03", "boundary", "按送达日期而不是下单日期统计2025年4月商品销售额。", plan(time_field="delivered_at", time_range=april)),
        EvaluationCase("B04", "isolation", "4月商品销售额多少？", plan(intent="clarify", time_range=None, clarification="请补充年份", missing_slots=["year"], pending_intent="aggregate", month=4), practice=False),
    ]
    grouping = ["category", "seller"]
    leader = plan(intent="rank", group_by=grouping, top_n=1)
    follow = plan(intent="details", metric_ids=[], source_step=1, bind_mode="group", detail_level="items")
    joint = [
        EvaluationCase("J01", "joint", f"{period}，按品类和商家联合汇总已送达商品销售额和去重订单数。", plan(group_by=grouping, metric_ids=["sales", "order_count"])),
        EvaluationCase("J02", "joint", f"{period}，每个品类内分别按已送达商品销售额列前三商家，保留并列。", plan(intent="rank", group_by=grouping, rank_within="category", top_n=3)),
        EvaluationCase("J03", "joint", f"{period}，按月份和品类查看已送达商品销售额与订单数。", plan(group_by=["month", "category"], metric_ids=["sales", "order_count"])),
        EvaluationCase("J04", "joint", "按品类和商家联合比较2025年4月与3月已送达商品销售额，以3月为固定基期。", plan(intent="compare", group_by=grouping, time_range=april, comparison_range=march, comparison_mode="fixed")),
        EvaluationCase("J05", "joint", "比较2025年4月与3月已送达商品额，各品类内按商家变化额升序展示前三名及并列，同时列出品类内贡献和全范围贡献，以3月为固定基期。", plan(intent="contribution", group_by=grouping, time_range=april, comparison_range=march, comparison_mode="fixed", contribution_within="category", contribution_direction="decrease", top_n=3)),
        EvaluationCase("J06", "joint", f"{period}，找到已送达商品销售额最高的品类与商家组合，再查看该组合的商品明细；并列时先让我选择。", plan(intent="workflow", metric_ids=[], steps=[leader, follow])),
    ]
    return original if suite == "original" else extended if suite == "extended" else joint if suite == "joint" else original + extended + joint


def assert_plan(actual: AnalysisPlan, expected: AnalysisPlan) -> None:
    assert actual.intent == expected.intent, f"意图：{actual.intent} != {expected.intent}"
    if expected.intent == "workflow":
        assert len(actual.steps) == len(expected.steps), "工作流步骤数量不符"
        for actual_step, expected_step in zip(actual.steps, expected.steps):
            assert_plan(actual_step, expected_step)
        return
    if expected.intent in {"clarify", "unsupported"}:
        assert actual.clarification
        if expected.missing_slots:
            assert actual.missing_slots == expected.missing_slots and actual.pending_intent == expected.pending_intent and actual.month == expected.month, "缺少可补全的澄清槽位"
        return
    if expected.intent == "explain":
        assert actual.explanation_topic == expected.explanation_topic
        return
    for field in ["dataset_id", "metadata_version", "dimension", "group_by", "rank_within", "contribution_within", "contribution_direction", "time_range", "comparison_range", "min_group_count", "time_field", "order_statuses", "order_where", "item_where", "selected_order_id", "detail_level", "source_step", "bind_field", "bind_mode", "source_row", "source_partition", "period_mode", "comparison_mode"]:
        assert getattr(actual, field) == getattr(expected, field), f"{field}: {getattr(actual, field)} != {getattr(expected, field)}"
    assert set(expected.metric_ids) <= set(actual.metric_ids), f"指标：{actual.metric_ids} != {expected.metric_ids}"
    assert {f.field: set(f.values) for f in actual.filters} == {f.field: set(f.values) for f in expected.filters}, "过滤条件不符"
    if expected.intent in {"rank", "contribution"}:
        assert actual.top_n == expected.top_n and actual.metric_ids[0] == expected.metric_ids[0], "排名指标或名次范围不符"
