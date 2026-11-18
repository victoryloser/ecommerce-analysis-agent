"""比较入口、分阶段证据和可恢复报告；原分析会话的查询状态保持独立。"""
import json
from uuid import uuid4
import zipfile

from app.config import RUNS_DIR
from app.evidence import write_artifact
from app.grouping import DIMENSION_LABELS
from app.models import AnalysisPlan, ComparisonRequest
from app.metadata import COMPARISON_METRICS
from app.plan_update import replay_intent
from app.semantic import PlanError, validate_plan
from app.session import Session
from app.session_store import snapshot_identity
from app.snapshots import implementation_hash
from app.snapshot_diff import run_diff
from app.trace import TurnTrace
from app.answers import label


def fixed_plan(plan):
    return plan.model_copy(deep=True, update={"period_mode": "explicit_range", "comparison_mode": "fixed" if plan.comparison_range else "none",
        "source_step": None, "source_row": 0, "bind_field": None, "bind_mode": "field", "source_partition": None})


def comparison_text(state):
    stages = state["stages"]
    labels = {"source": "来源重算", "target": "目标重算", "diff": "差异核验"}
    if state["status"] != "completed":
        return "比较尚未完成，已完成结果已保存。\n\n" + "\n\n".join(f"{labels[k]}：{v['status']} {v.get('error', '')}" for k, v in stages.items())
    report = stages["diff"]["report"]
    business = report["business_diff"]
    if state["contract_changes"]:
        conclusion = "两版计算口径不同，保留各自数值和记录差异，不能直接认定为数据修订。"
    elif business["d2_equivalent"]:
        conclusion = "已核验标准记录、逐订单支付投影和质量状态；允许合法支付拆分，当前指定经营指标的值及完整度一致。"
    elif business["amount_revision_only"]:
        conclusion = f"检测到既有记录的金额字段变化：商品修订涉及 {business['revised_item_orders']} 笔订单、{business['revised_item_keys']} 个明细键；{business['changed_orders']} 笔订单的金额投影改变。"
    elif report["diff_rows"] == 0:
        conclusion = "标准记录和质量依据一致；未确认指标仍不能据此认定数值相等。"
    else:
        conclusion = f"检测到记录或质量变化：{business['changed_orders']} 笔订单的金额/状态投影改变，{report['quality_diff']['changed']} 项质量依据改变。"
    if any(r[side]["state"] == "unknown" for r in report["metric_comparisons"] for side in ("before", "after")):
        conclusion += " 当前分析有未确认的指标值或筛选归属，对应总额差不能由小计推算；完整指标继续展示。"
    if report["scope_changes"]:
        conclusion += " 本次实际分析范围或对象发生变化，详见范围差异；两侧数值不作为同口径修订相减。"
    lines = [conclusion, f"来源：{state['source_label']}；目标：{state['target_label']}。",
             "比较范围按完整候选集核验，包含 Top N 和展示门槛以外的分组；当前页面只预览，下载包保留全量明细。"]
    for side, title in (("source", "原版范围"), ("target", "新版范围")):
        plan = stages[side]["effective_plan"]
        period = plan["time_range"]
        filters = "；".join(f"{label(f['field'])}：{','.join(f['values'])}" for f in plan["filters"])
        lines.append(f"{title}：{label(plan['time_field'])} [{period['start']}, {period['end']})；{filters or '无实体筛选'}。")
        if plan.get("group_by"):
            lines.append("联合分组：" + " × ".join(DIMENSION_LABELS[d] for d in plan["group_by"]) +
                         (f"；按{DIMENSION_LABELS[plan['rank_within']]}分别排名" if plan.get("rank_within") else "") +
                         ("；另列品类内贡献" if plan.get("contribution_within") else "") + "。")
    if report["scope_changes"]:
        lines.append("范围变更详情：\n\n" + "\n\n".join(f"{label(field)}：{json.dumps(values[0], ensure_ascii=False)} → {json.dumps(values[1], ensure_ascii=False)}" for field, values in report["scope_changes"].items()))
    if report["reconciliation"]:
        lines.append("| 指标 / 分组 | 原版基期 | 原版本期 | 新版基期 | 新版本期 | 月间差额修订影响 | 核验 |\n|---|---:|---:|---:|---:|---:|---|")
    for result in report["reconciliation"][:10]:
        values = ["未确认" if v is None else str(v) for v in result["values"]]
        effect = "未确认" if result["revision_effect"] is None else result["revision_effect"]
        status = "通过" if result["verified"] is True else "暂不能确认" if result["verified"] is None else "不一致"
        lines[-1] += f"\n| {label(result['metric'])} / {result['entity_id']} | " + " | ".join(values) + f" | {effect} | {status} |"
    eligibility = [r for r in report["metric_comparisons"] if r.get("membership") and (r["membership"].get("eligibility_entered_count") or r["membership"].get("eligibility_exited_count"))]
    for row in eligibility[:10]:
        bridge = row["membership"]
        lines.append(f"{row['period']} · {label(row['metric'])} / {row['entity_name']}：金额资格恢复 {bridge['eligibility_entered_value']} 元，资格退出 {bridge['eligibility_exited_value']} 元；这是报表准入变化，不是订单删除或冲突金额估计。")
    if any(r.get("membership") and not r["membership"].get("attribution_complete", True) for r in report["metric_comparisons"]):
        lines.append("部分订单范围归属未知，虽可核对报表贡献差额，仍不能将这些差额确定归因为业务范围进入或退出；详见归属未确认差额。")
    lines.append("范围进入/退出是查询成员变化，不代表新增/取消交易；共同键金额修订与进入范围金额不重复相加。")
    return "\n\n".join(lines)


def prepare_comparison(agent, session, request):
    request = ComparisonRequest.model_validate(request)
    if request.target_dataset_id not in agent.executors:
        raise PlanError("请选择已导入的目标快照")
    step = None
    original = None
    if request.source == "current_result":
        record = session.analysis_evidence
        if record.get("type") != "query":
            raise PlanError("请先选择成功分析结果，或选择当前分析意图")
        if request.source_record_id and request.source_record_id != record["record_id"]:
            raise PlanError("所选原分析记录已经改变")
        base = AnalysisPlan.model_validate(record["plan"])
        original = record["record_id"]
    else:
        if request.source_record_id:
            raise PlanError("当前意图比较不同时绑定历史记录")
        if session.working_plan is None:
            raise PlanError("当前没有分析意图")
        base = session.working_plan.model_copy(deep=True)
        if base.intent == "workflow":
            step = (session.focus_step or 1) - 1
            if request.mode == "fixed_scope":
                chosen = base.steps[step]
                if chosen.source_step:
                    raise PlanError("动态步骤固定对象比较需先选择该步骤的成功结果")
                base, step = chosen, None
    selected = base.steps[step] if step is not None else base
    if selected.intent not in {"aggregate", "rank", "compare", "contribution", "details"}:
        raise PlanError("版本比较支持汇总、排名、期间比较、贡献及明细分析")
    if request.mode == "fixed_scope":
        base = fixed_plan(base)
    source = agent.executors[session.dataset_id].manifest
    target = agent.executors[request.target_dataset_id].manifest
    changes = {k: [source.get(k), target.get(k)] for k in ("schema_sha256", "metadata_sha256", "quality_rule_version") if source.get(k) != target.get(k)}
    state = {"comparison_id": uuid4().hex, "status": "pending", "request": request.model_dump(),
        "source_dataset_id": session.dataset_id, "target_dataset_id": request.target_dataset_id,
        "source_snapshot": snapshot_identity(source), "target_snapshot": snapshot_identity(target),
        "source_label": source.get("business_label", source["name"]), "target_label": target.get("business_label", target["name"]),
        "original_record_id": original, "source_session_id": session.session_id,
        "intent": base.model_dump(mode="json"), "selected_step": step,
        "implementation_sha256": implementation_hash(), "contract_changes": changes,
        "metric_contract": {metric: COMPARISON_METRICS[metric] for metric in selected.metric_ids},
        "source_files_equal": sorted(r["sha256"] for r in source["sources"]) == sorted(r["sha256"] for r in target["sources"]) if source["sources"] and target["sources"] else None,
        "lineage": {"source_parent": source.get("parent_dataset_id"), "target_parent": target.get("parent_dataset_id"),
                    "source_origin": source.get("data_origin"), "target_origin": target.get("data_origin"),
                    "target_change_note": target.get("change_note")},
        "stages": {name: {"status": "pending"} for name in ("source", "target", "diff")}}
    return state


def compare(agent, session, request=None, *, resume=False):
    if session is None:
        raise PlanError("请先建立或恢复来源分析")
    if resume:
        state = session.comparison_state
        if not state:
            raise PlanError("没有待继续的版本比较")
    else:
        state = prepare_comparison(agent, session, request)
        session.comparison_state = state
    for side in ("source", "target"):
        manifest = agent.executors[state[side + "_dataset_id"]].manifest
        if snapshot_identity(manifest) != state[side + "_snapshot"]:
            raise PlanError("比较绑定的快照身份已改变，请新建比较")
    if state["implementation_sha256"] != implementation_hash():
        raise PlanError("计算实现已改变，请以同一实现重新比较两侧")
    if resume and state["status"] == "completed":
        from app.orchestrator import Turn
        return Turn(comparison_text(state), state)
    directory = RUNS_DIR / session.dataset_id / "comparisons" / state["comparison_id"]
    directory.mkdir(parents=True, exist_ok=True)
    state["report_file"] = str(directory / "report.json")
    def save():
        temporary = directory / "report.tmp"
        temporary.write_text(json.dumps(state, ensure_ascii=False, default=str, indent=2), encoding="utf-8")
        temporary.replace(directory / "report.json")
        agent.checkpoint(session)
    save()
    for side in ("source", "target"):
        stage = state["stages"][side]
        if stage["status"] == "completed":
            continue
        executor = agent.executors[state[side + "_dataset_id"]]
        plan = replay_intent(AnalysisPlan.model_validate(state["intent"]), executor.manifest)
        fresh = Session(executor.manifest["dataset_id"])
        # 失败的工作流保存自己的检查点，恢复后只执行未完成依赖。
        if stage.get("session_id") and agent.store:
            fresh = agent.store.load(stage["session_id"], executor.manifest)
        stage.update(status="running", session_id=fresh.session_id)
        save()
        try:
            validate_plan(plan, executor.manifest)
            with TurnTrace(fresh.session_id, fresh.dataset_id, "版本比较同口径重算", "snapshot-comparison") as trace:
                if plan.intent == "workflow":
                    turn = agent.run_workflow(fresh, plan, "版本比较同口径重算", 0, trace)
                    chosen = fresh.workflow_tasks[state["selected_step"]]
                    record = chosen["evidence"]
                    if chosen["status"] != "completed" or record.get("type") != "query":
                        raise PlanError(chosen["reason"] or "选定步骤尚未完成")
                else:
                    fresh.working_plan = plan
                    turn = agent.run_plan(fresh, plan, "版本比较同口径重算", 0, trace,
                        provenance={"comparison_id": state["comparison_id"], "source_record_id": state["original_record_id"], "source_session_id": session.session_id})
                    record = turn.evidence
                    fresh.remember("版本比较同口径重算", turn.text)
                fresh.last_evidence = turn.evidence
                agent.checkpoint(fresh)
                stage["evidence"] = record
                if record["result"]["status"] not in {"ok", "empty"}:
                    raise PlanError(record["result"].get("error") or "单侧查询未完成")
                stage.update(status="completed", effective_plan=record["plan"], error=None)
        except ValueError as error:
            stage.update(status="failed", error=str(error))
        save()
    if all(state["stages"][side]["status"] == "completed" for side in ("source", "target")) and state["stages"]["diff"]["status"] != "completed":
        stage = state["stages"]["diff"]
        stage.update(status="running")
        save()
        paths = [agent.executors[state[side + "_dataset_id"]].path for side in ("source", "target")]
        plans = [AnalysisPlan.model_validate(state["stages"][side]["effective_plan"]) for side in ("source", "target")]
        report = run_diff(paths, plans, directory, comparable=not state["contract_changes"])
        stage.update(status=report["status"], report=report, error=report.get("error"))
        save()
    state["status"] = "completed" if all(stage["status"] == "completed" for stage in state["stages"].values()) else "incomplete"
    text = comparison_text(state)
    with TurnTrace(session.session_id, session.dataset_id, "比较数据版本", "snapshot-comparison") as trace:
        artifact = write_artifact(session, agent.executors[session.dataset_id].manifest, "snapshot_comparison", "比较数据版本",
            {"comparison_id": state["comparison_id"], "source_snapshot": state["source_snapshot"], "target_snapshot": state["target_snapshot"],
             "report_file": state["report_file"], "status": state["status"], "original_record_id": state["original_record_id"],
             "source_record_id": state["stages"]["source"].get("evidence", {}).get("record_id"),
             "target_record_id": state["stages"]["target"].get("evidence", {}).get("record_id")}, trace)
    state["artifact_id"] = artifact["record_id"]
    session.remember("比较数据版本", text)
    state["transcript_turn_index"] = len(session.transcript) // 2 - 1
    save()
    if state["status"] == "completed":
        state["bundle_file"] = str(directory / "comparison.zip")
        save()
        with zipfile.ZipFile(state["bundle_file"], "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ("report.json", "diff.json", "differences.csv"):
                archive.write(directory / name, name)
            for side in ("source", "target"):
                archive.writestr(side + "_evidence.json", json.dumps(state["stages"][side]["evidence"], ensure_ascii=False, indent=2, default=str))
    from app.orchestrator import Turn
    return Turn(text, state)
