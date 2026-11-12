"""新增能力的真实模型定向验收，期望数值由来源表独立计算。"""
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from app.config import DATASETS, ROOT, RUNS_DIR
from app.demo import practice_plans
from app.executor import Executor
from app.models import AnalysisPlan, QueryResult, TimeRange
from app.oracle import SourceOracle, check_rows
from app.orchestrator import AnalysisAgent
from app.providers import ChatCompletionsProvider
from app.session import Session
from app.session_store import SessionStore


def comparison_rows(oracle, plan):
    current = {r["entity_id"]: r for r in oracle.evaluate(plan.model_copy(update={"intent":"aggregate","comparison_range":None}))}
    previous = {r["entity_id"]: r for r in oracle.evaluate(plan.model_copy(update={"intent":"aggregate","time_range":plan.comparison_range,"comparison_range":None}))}
    rows = []
    for key in sorted(current.keys() | previous.keys()):
        row = {"entity_id":key,"entity_name":(current.get(key) or previous[key])["entity_name"]}
        for metric in plan.metric_ids:
            c, p = current.get(key,{}).get(metric,0), previous.get(key,{}).get(metric,0)
            row.update({metric+"_current":c,metric+"_previous":p,metric+"_change":c-p,metric+"_rate":(c-p)/p if p else None})
        rows.append(row)
    return rows


def check_contribution(oracle, evidence, expected):
    actual_plan=AnalysisPlan.model_validate(evidence["plan"])
    assert actual_plan.intent=="contribution" and actual_plan.dimension=="category"
    rows=comparison_rows(oracle,expected)
    total=sum((r["sales_change"] for r in rows),Decimal(0))
    for row in rows:
        row.update(total_change=total,contribution_rate=row["sales_change"]/total if total else None)
    rows.sort(key=lambda row:(-row["sales_change"],row["entity_id"]))
    check_rows(QueryResult.model_validate(evidence["result"]),rows)


def main():
    provider=ChatCompletionsProvider()
    cases=[]
    report={"mode":"live-model-extended","model":provider.model,"created_at":datetime.now(timezone.utc).isoformat(),
        "implementation_sha256":hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/'app').glob('*.py')))).hexdigest(),
        "total":16,"completed":0,"passed":0,"status":"running","cases":cases}
    path=RUNS_DIR/f"extended-live-{uuid4().hex}.json"
    store=SessionStore(RUNS_DIR/f"extended-memory-{uuid4().hex}.sqlite3")
    for dataset in DATASETS:
        executor=Executor(dataset)
        oracle=SourceOracle(dataset)
        agent=AnalysisAgent(provider,{dataset:executor},store)
        plans=practice_plans(executor.manifest)
        comparison=plans["Q03"].model_copy(update={"dimension":"category","metric_ids":["sales","order_count"]})
        contribution=plans["Q03"].model_copy(update={"intent":"contribution","dimension":"category"})

        def perform(name,question,check):
            session=Session(dataset)
            evidence={}
            failure=None
            try:
                turn=agent.respond(session,question)
                evidence=turn.evidence
                check(session,turn)
            except Exception as error:
                failure=str(error) if isinstance(error,AssertionError) else type(error).__name__
                raise
            finally:
                cases.append({"dataset_id":dataset,"stage":name,"question":question,"passed":failure is None,
                              "failure":failure,"evidence_id":evidence.get("record_id")})
                report.update(completed=len(cases),passed=sum(c["passed"] for c in cases),status="failed" if failure else "running")
                path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
                print(dataset,name,"PASS" if failure is None else "FAIL",flush=True)

        def workflow_check(session,turn):
            assert turn.evidence["type"]=="workflow",turn.text
            steps=turn.evidence["steps"]
            total=next(step for step in steps if step.get("plan",{}).get("intent")=="compare" and step["plan"]["dimension"]=="total")
            check_rows(QueryResult.model_validate(total["result"]),oracle.evaluate(plans["Q03"]))
            change=next(step for step in steps if step.get("plan",{}).get("intent")=="contribution")
            check_contribution(oracle,change,contribution)
            assert any(step.get("plan",{}).get("explanation_topic")=="causality" for step in steps)
        perform("compound-analysis","先计算2025年4月相对3月的已送达商品销售额环比，再计算各品类对整体净增长的贡献，最后说明能否把增长归因于促销。",workflow_check)

        def grouped_check(session,turn):
            assert session.last_successful_plan.intent=="compare" and session.last_successful_plan.dimension=="category",turn.text
            check_rows(QueryResult.model_validate(turn.evidence["result"]),comparison_rows(oracle,comparison))
        perform("grouped-multi-metric","按品类比较2025年4月与3月的已送达商品销售额和订单数，输出两期值、差额和变化率，不排名。",grouped_check)

        def filtered_check(session,turn):
            expected=Decimal(0)
            for order in oracle.orders:
                if order["order_status"]!="delivered" or not "2025-04-01"<=str(order["purchase_at"])[:10]<"2025-05-01": continue
                items=oracle.items[order["order_id"]]
                total=sum((Decimal(str(item["price"])) for item in items),Decimal(0))
                review=oracle.reviews.get(order["order_id"])
                if total>=200 or (review is not None and review<=2):
                    expected+=sum((Decimal(str(item["price"])) for item in items if 50<=Decimal(str(item["price"]))<=300),Decimal(0))
            check_rows(QueryResult.model_validate(turn.evidence["result"]),[{"sales":expected}])
        perform("boolean-numeric-filters","2025年4月已送达订单中，整单商品金额至少200元或者评价分数不超过2分；只累计其中成交单价在50至300元（含边界）的商品销售额，不含运费。",filtered_check)

        def canceled_check(session,turn):
            assert session.last_successful_plan.order_statuses==["cancelled"],turn.text
            expected=sum(o["order_status"]=="cancelled" for o in oracle.orders)
            check_rows(QueryResult.model_validate(turn.evidence["result"]),[{"order_count":expected}])
        perform("explicit-status","使用材料覆盖的全部下单期间，统计状态为cancelled的订单数，不要统计delivered。",canceled_check)

        def reconciliation_check(session,turn):
            assert turn.evidence["plan"]["intent"]=="diagnose" and turn.evidence["plan"]["diagnosis"]=="reconciliation",turn.text
            expected=set()
            for order in oracle.orders:
                if order["order_status"]=="delivered" and "2025-04-01"<=str(order["purchase_at"])[:10]<"2025-05-01":
                    oid=order["order_id"]
                    amount=sum((Decimal(str(i["price"]))+Decimal(str(i["freight_value"])) for i in oracle.items[oid]),Decimal(0))
                    if abs(oracle.payments[oid]-amount)>Decimal('.01'):expected.add(oid)
            result=turn.evidence["result"]
            assert result["status"] in {"ok","empty"}
            assert {row[result["columns"].index("order_id")] for row in result["rows"]}==expected
        perform("payment-reconciliation","对2025年4月下单且已送达订单逐单对账：支付总额与商品金额加运费不一致超过0.01元的订单有哪些？不要凭空解释原因。",reconciliation_check)

        def details_check(session,turn):
            assert turn.evidence["plan"]["intent"]=="details" and turn.evidence["plan"]["detail_level"]=="orders",turn.text
            expected=sorted([o for o in oracle.orders if o["order_status"]=="delivered" and "2025-04-01"<=str(o["purchase_at"])[:10]<"2025-05-01"],key=lambda row:row["order_id"])
            result=turn.evidence["result"]
            assert [r[result["columns"].index("order_id")] for r in result["rows"]]==[r["order_id"] for r in expected[:500]]
            for values in result["rows"]:
                row=dict(zip(result["columns"],values)); oid=row["order_id"]
                assert Decimal(str(row["payment_amount"]))==oracle.payments[oid]
                assert Decimal(str(row["order_sales"]))==sum((Decimal(str(item["price"])) for item in oracle.items[oid]),Decimal(0))
        perform("order-detail","逐笔列出2025年4月已送达订单明细，包含订单ID、整单商品金额、运费、支付金额和评价分数，不要汇总成一行。",details_check)

        def dependent_check(session,turn):
            assert turn.evidence["type"]=="workflow",turn.text
            first=next(step for step in turn.evidence["steps"] if step.get("plan",{}).get("intent")=="contribution")
            check_contribution(oracle,first,contribution)
            top=first["result"]["rows"][0][0]
            last=next(step for step in turn.evidence["steps"] if step.get("plan",{}).get("dimension")=="seller")
            actual=AnalysisPlan.model_validate(last["plan"])
            assert any(f.field=="category_id" and f.values==[top] for f in actual.filters)
            check_rows(QueryResult.model_validate(last["result"]),oracle.evaluate(actual))
        perform("dependent-drill","先找2025年4月相对3月已送达商品销售额净增长贡献最大的品类，然后在这个实际查出的品类中，查询2025年4月销售额前三的商家，保留并列。不要预先猜品类ID。",dependent_check)

        old=Session(dataset)
        amount=oracle.evaluate(plans["Q02"].model_copy(update={"time_range":TimeRange(start="2025-04-01",end="2025-05-01")}))[0]["sales"]
        old.remember("2025年4月商品销售额",f"本次来源表核对结果为{amount}元。")
        store.save(old,executor.manifest)
        def history_check(session,turn):
            assert turn.evidence["type"]=="history",turn.text
            assert any(hit["session_id"]==old.session_id for hit in turn.evidence["hits"])
            assert str(amount) in turn.text
        perform("historical-retrieval","检索以前会话里2025年4月商品价款的分析结果，不要重新查询业务数据。",history_check)
    report["status"]="passed"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(path,flush=True)


if __name__=="__main__":
    main()
