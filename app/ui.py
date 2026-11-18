import argparse
import json

import httpx

from app.config import DATASETS, MAX_ROWS
from app.executor import Executor
from app.orchestrator import AnalysisAgent
from app.presentation import example_questions, present
from app.answers import STATUS_LABELS
from app.providers import ChatCompletionsProvider, ModelConfigurationError, model_error_message
from app.session import Session
from app.models import AnalysisPlan
from app.clarification import choices
from app.drilldown import TARGETS, selected_row_index
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace, workspace_catalog
from app.presentation import display_evidence
from app.charts import numeric_columns, render_charts, CLICK_SCRIPT
from app.data_upload import registered_datasets, inspect_mapping, import_upload
from app.delivery import is_delivery_request, refresh_delivery
from app.review_ui import add_review_panel


def build_ui():
    # Windows spawn 会重载入口模块，界面依赖留在主进程加载。
    import gradio as gr
    import pandas as pd

    datasets = registered_datasets()
    datasets.update({key: name for key, name in DATASETS.items() if key in datasets})
    executors = {dataset: Executor(dataset) for dataset in datasets}
    for dataset_id, executor in executors.items():
        executor.manifest["name"] = datasets[dataset_id]
    initial_dataset = next(iter(datasets), None)
    initial_manifest = executors[initial_dataset].manifest if initial_dataset else None
    store = SessionStore()
    agent = AnalysisAgent(None, executors, store)
    workspace = SQLWorkspace(executors, store)

    def provider():
        if agent.provider is None:
            agent.provider = ChatCompletionsProvider()
        return agent.provider

    def current_session(session, dataset_id):
        if dataset_id not in executors:
            raise gr.Error("请先上传并选择要分析的数据。")
        if session is None:
            return Session(dataset_id)
        if session.dataset_id != dataset_id:
            raise ValueError("当前会话绑定了另一套材料，请先切换材料或恢复对应会话")
        return session

    def archives(dataset_id, value=None):
        return gr.Dropdown(choices=[(f"{r['title']} · {r['updated_at'][:16]}", r["session_id"])
                                    for r in store.list(dataset_id)], value=value)

    def result_components(evidence):
        view = present(evidence)
        options = choices(AnalysisPlan.model_validate(evidence["plan"]), executors[evidence["dataset_id"]].manifest) if evidence.get("type") == "clarify" else []
        if evidence.get("type") == "workflow":
            pending = next((step for step in evidence["plan"]["steps"] if step["intent"] == "clarify"), None)
            options = choices(AnalysisPlan.model_validate(pending), executors[evidence["dataset_id"]].manifest) if pending else []
        frame = pd.DataFrame(view.chart, columns=["分组", "数值", *([view.chart_series] if view.chart_series else [])])
        return (
            gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=bool(view.headers)),
            gr.BarPlot(value=frame, x="分组", y="数值", y_title=view.chart_title, title=view.chart_title, visible=bool(view.chart) and view.chart_kind == "bar", sort="-y"),
            gr.LinePlot(value=frame, x="分组", y="数值", color=view.chart_series, y_title=view.chart_title, title=view.chart_title, visible=bool(view.chart) and view.chart_kind == "line", sort="x"),
            gr.DownloadButton(value=view.csv_file, visible=view.csv_file is not None),
            gr.DownloadButton(value=view.evidence_file, visible=view.evidence_file is not None),
            gr.Dropdown(choices=options, value=None, visible=bool(options)),
        )

    def submit(question, history, session, dataset_id, practice_context):
        session = current_session(session if session and session.dataset_id == dataset_id else None, dataset_id)
        if is_delivery_request(question):
            session, _ = refresh_delivery(agent, session, question)
            target = session.dataset_id
            selection = gr.skip()
            if target != dataset_id:
                datasets[target] = executors[target].manifest["name"]
                selection = gr.Dropdown(choices=[(name, key) for key, name in datasets.items()], value=target)
            return ("", session.transcript, session, session.last_evidence, *result_components(session.last_evidence),
                    archives(target, session.session_id), selection)
        try:
            provider()
            turn = agent.respond(session, question, practice_context)
        except (httpx.HTTPError, ModelConfigurationError) as error:
            gr.Warning(model_error_message(error))
            return question, session.transcript, session, gr.skip(), *(gr.skip() for _ in result_outputs), archives(dataset_id, session.session_id), gr.skip()
        return "", session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(dataset_id, session.session_id), gr.skip()

    def switch_dataset(dataset_id, session):
        questions = example_questions(executors[dataset_id].manifest) if dataset_id in executors else []
        if session and session.dataset_id == dataset_id:
            return (session.transcript, session, session.last_evidence, gr.Dropdown(choices=questions, value=None),
                    *result_components(session.last_evidence), archives(dataset_id, session.session_id), "SELECT * FROM analysis_result", "[]", "analysis_result", "")
        return ([], Session(dataset_id) if dataset_id in executors else None, {}, gr.Dropdown(choices=questions, value=None),
                *result_components({}), archives(dataset_id), "SELECT * FROM analysis_result", "[]", "analysis_result", "")

    def dataset_ready(dataset_id):
        ready = dataset_id in executors
        return gr.Column(visible=ready), gr.Markdown(visible=not ready)

    def restore_session(session_id, dataset_id):
        restored = store.load(session_id, executors[dataset_id].manifest)
        sql = restored.sql_state
        return (restored.transcript, restored, restored.last_evidence, *result_components(restored.last_evidence), archives(dataset_id, session_id),
                sql.get("sql", "SELECT * FROM analysis_result"), json.dumps(sql.get("parameters", []), ensure_ascii=False),
                sql.get("scope", "analysis_result"), sql.get("explanation", ""))

    def new_session(dataset_id):
        return [], Session(dataset_id), {}, *result_components({}), archives(dataset_id), "SELECT * FROM analysis_result", "[]", "analysis_result", ""

    def drill(session, target, evt: gr.SelectData):
        agent.drill(session, selected_row_index(session.last_evidence, evt.row_value), target)
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def back(session):
        agent.back(session)
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def catalog_for_sql(session, dataset_id, scope):
        return workspace_catalog(current_session(session, dataset_id), scope, executors[dataset_id].manifest)

    def draft_sql(question, session, dataset_id, scope):
        session = current_session(session, dataset_id)
        draft = workspace.draft(session, question, scope, provider())
        return draft.sql, draft.explanation, session, archives(dataset_id, session.session_id), "[]"

    def run_sql(sql, parameters, session, dataset_id, scope):
        session = current_session(session, dataset_id)
        values = json.loads(parameters)
        if not isinstance(values, list) or any(isinstance(v, (list, dict)) for v in values):
            raise ValueError("SQL参数应为标量值组成的JSON数组")
        evidence = workspace.execute(session, sql, values, scope)
        return session.transcript, session, evidence, *result_components(evidence), archives(dataset_id, session.session_id)

    def refresh_visuals(evidence):
        columns = numeric_columns(evidence)
        selected = [c for c in columns if c not in {"sample_orders", "reviewed_orders", "delayed_orders", "missing_timing_orders"}][:6]
        from app.answers import label
        return gr.CheckboxGroup(choices=[(label(c), c) for c in columns], value=selected), render_charts(evidence, selected), gr.DownloadButton(value=None, visible=False), "", None

    def result_controls(evidence, session):
        current = display_evidence(evidence)
        result = current.get("result") or {}
        ready = result.get("status") in {"ok", "empty"}
        plan = current.get("plan", {})
        can_drill = ready and current.get("type") == "query" and plan.get("intent") in {"aggregate", "rank", "compare", "contribution", "details", "diagnose"}
        workflow = session.selected_workflow() if session else {}
        can_retry = (current.get("type") == "query" and result.get("status") in {"timeout", "error"}
                     and session and session.working_plan and session.working_plan.model_dump(mode="json") == plan)
        page_note = ""
        if result.get("rows") and (result["truncated"] or result["row_offset"]):
            page_note = f"显示第 {result['row_offset']+1}–{result['row_offset']+len(result['rows'])} 条"
            if not result["truncated"]:
                page_note += "，已到末页"
        return (gr.Row(visible=can_drill), gr.Row(visible=workflow.get("type") == "workflow"),
                gr.Row(visible=ready and (result.get("truncated", False) or result.get("row_offset", 0) > 0 or len(result.get("rows", [])) >= 100)),
                gr.Button(visible=ready), gr.Accordion(visible=bool(numeric_columns(evidence))),
                gr.Button(visible=can_retry and not workflow), result.get("page_size") or MAX_ROWS, page_note)

    def catalog_table(catalog):
        rows = []
        for table, info in (catalog or {}).get("tables", {}).items():
            if "fields" in info:
                rows.extend([table, field, detail["meaning"], detail["type"]] for field, detail in info["fields"].items())
            else:
                from app.answers import label
                rows.extend([table, column, label(column), ""] for column in info.get("columns", []))
        return gr.Dataframe(value={"headers": ["数据表", "字段", "说明", "类型"], "data": rows}, visible=bool(rows))

    def validation_text(record):
        if not record:
            return ""
        return "核对完成，结果与上次分析一致。" if record["matched"] else f"核对完成，有 {record['differing_rows']} 行差异，请检查查询条件和计算方式。"

    def mapping_table(mapping):
        from app.metadata import FIELD_MEANINGS, TABLE_GRAINS
        rows = []
        for table, config in json.loads(mapping)["tables"].items():
            rows.extend([table, field, TABLE_GRAINS[table] + " · " + FIELD_MEANINGS.get(field, "数量"), config["file"], config["sheet"] or "", source]
                        for field, source in config["columns"].items())
        return gr.Dataframe(value={"headers": ["业务表", "标准字段", "含义", "来源文件", "工作表（CSV留空）", "来源列名"], "data": rows}, visible=True)

    def save_mapping(frame):
        mapping = {"tables": {}}
        for table, field, _, filename, sheet, source in frame.fillna("").itertuples(index=False, name=None):
            config = mapping["tables"].setdefault(table, {"file": filename, "sheet": sheet or None, "columns": {}})
            if config["file"] != filename or config["sheet"] != (sheet or None):
                raise gr.Error("同一业务表的字段需来自同一文件和工作表。")
            config["columns"][field] = source
        return json.dumps(mapping, ensure_ascii=False)

    def select_step(session, index):
        from app.models import QueryResult
        root = session.selected_workflow()
        if root.get("type") != "workflow":
            raise ValueError("当前不是多步骤分析")
        if not 1 <= int(index) <= len(root["steps"]):
            raise ValueError("步骤编号超出当前任务范围")
        selected = root["steps"][int(index)-1]
        if session.view_focus.get("kind") == "inspection":
            session.view_focus["step"] = int(index)
        else:
            session.focus_step = int(index)
        if selected.get("type") == "query" and selected["result"]["status"] in {"ok", "empty"}:
            session.last_successful_plan = AnalysisPlan.model_validate(selected["plan"])
            session.last_result = QueryResult.model_validate(selected["result"])
            session.analysis_evidence = selected
            session.view_focus["record_id"] = selected["record_id"]
        session.last_evidence = selected
        store.save(session, executors[session.dataset_id].manifest)
        return session, selected, *result_components(selected)

    def replay_current(session, target):
        fresh, turn = agent.replay(session, target)
        return fresh.transcript, fresh, turn.evidence, *result_components(turn.evidence), archives(target, fresh.session_id), target

    def compare_current(session, target, mode, source, resume=False):
        from app.models import ComparisonRequest
        from app.answers import label
        turn = agent.compare(session, ComparisonRequest(target_dataset_id=target, mode=mode, source=source) if not resume else None, resume=resume)
        state = turn.evidence
        report = state["stages"]["diff"].get("report", {})
        labels = {"known": "已确认", "unknown": "未知", "absent": "无此分组", "not_applicable": "无适用样本"}
        table = [["本期" if r["period"] == "current" else "基期", r["entity_id"], label(r["metric"]), r["before"]["value"], labels[r["before"]["state"]],
                  r["after"]["value"], labels[r["after"]["state"]], r["delta"],
                  (f"共同变化 {r['membership']['common_change']}；进入 {r['membership']['entered_value']}；退出 {r['membership']['exited_value']}；资格恢复 {r['membership'].get('eligibility_entered_value', 0)}；资格退出 {r['membership'].get('eligibility_exited_value', 0)}；归属未确认差额 {r['membership'].get('scope_uncertain_change', 0)}；未确认 {r['membership']['unknown_count']}" if r["membership"] else "按指标定义重算"),
                  "通过" if r["reconciled"] is True else "暂不能确认" if r["reconciled"] is None else "不一致",
                  "是" if r["displayed_before"] else "否", "是" if r["displayed_after"] else "否"]
                 for r in report.get("metric_comparisons", [])[:500]]
        changes = [[r["layer"], r["key"], r["change"], r["before"], r["after"], r["explanation"]] for r in report.get("preview", [])]
        stringify = lambda rows: [["—" if v is None else json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v) for v in row] for row in rows]
        return (session.transcript, session, turn.text,
            gr.Dataframe(value={"headers": ["期间", "分组", "指标", "原值", "原计算状态", "新值", "新计算状态", "差额", "范围成员分解", "勾稽", "原表可见", "新表可见"], "data": stringify(table)}, visible=bool(table)),
            gr.Dataframe(value={"headers": ["差异层次", "业务键", "变化", "原记录", "新记录", "解释"], "data": stringify(changes)}, visible=bool(changes)),
            gr.DownloadButton(value=state.get("bundle_file"), visible=state.get("status") == "completed"),
            gr.DownloadButton(value=state["report_file"], visible=True), state, archives(session.dataset_id, session.session_id))

    def remove_condition(session, field):
        from app.models import PlanUpdate
        turn = agent.update_conditions(session, PlanUpdate(operation="remove", field=field))
        session.remember("撤回条件：" + field, turn.text)
        store.save(session, executors[session.dataset_id].manifest)
        return session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(session.dataset_id, session.session_id)

    def inspect_quality(session, evidence):
        turn = agent.quality_details(session, specified=True)
        view = present(turn.evidence)
        return gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=True), gr.DownloadButton(value=view.csv_file, visible=True), turn.text, gr.Button(visible=True), gr.Accordion(visible=True), evidence.get("record_id")

    def reset_quality(evidence, parent_id):
        # A queued refresh of the same evidence must not hide a newly opened inspection.
        if parent_id and parent_id == evidence.get("record_id"):
            return (gr.skip(),) * 7
        return (gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False), "", None,
                gr.DownloadButton(value=None, visible=False), gr.Button(visible=False), gr.Accordion(visible=False, open=False))

    def inspect_source(session, evt: gr.SelectData):
        import json
        from app.config import RUNS_DIR
        if session.view_focus.get("kind") != "quality":
            raise ValueError("请先打开质量依据")
        record = json.loads((RUNS_DIR / session.dataset_id / (session.view_focus["record_id"] + ".json")).read_text(encoding="utf-8"))
        position = record["result"]["columns"].index("order_id")
        return agent.quality_sources(session, str(evt.row_value[position]))

    def chart_drill(session, target, evt: gr.SelectData):
        current = display_evidence(session.last_evidence)
        if current.get("type") != "query":
            raise ValueError("请选择结构化分析图表")
        result = current["result"]
        ids = [str(row[result["columns"].index("entity_id")]) for row in result["rows"]] if "entity_id" in result["columns"] else ["all"]
        row = ids.index(str(evt.value))
        agent.drill(session, row, target)
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def paginate(session, size, delta):
        turn = agent.page(session, delta, int(size))
        if turn.evidence["result"]["status"] not in {"ok", "empty"}:
            gr.Warning(turn.evidence["result"]["error"])
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def resume_analysis(session):
        turn = agent.resume_inspection(session)
        return session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(session.dataset_id, session.session_id)

    def full_export(session):
        record = agent.export(session)
        return gr.DownloadButton(value=record["csv_file"], visible=True), f"已生成完整结果，共 {record['row_count']} 行，可点击下载。"

    def verify_sql(sql, parameters, session, dataset_id, scope):
        return workspace.verify(current_session(session, dataset_id), sql, json.loads(parameters), scope)

    def inspect_upload(files, template="full"):
        structure, mapping = inspect_mapping(files, template)
        return structure, json.dumps(mapping, ensure_ascii=False, indent=2)

    def upload_dataset(files, mapping, name, parent=None, note="", origin="uploaded", template="full", amount_mode="line", status_map="{}"):
        version = {"parent_dataset_id": parent, "change_note": note, "data_origin": origin} if parent or note or origin != "uploaded" else {}
        mapping = json.loads(mapping)
        if template == "orders_items":
            mapping.update(template=template, currency="CNY", amount_mode=amount_mode, status_map=json.loads(status_map))
        manifest = import_upload(files, mapping, name, **version)
        dataset_id = manifest["dataset_id"]
        datasets[dataset_id] = manifest["name"]
        executors[dataset_id] = Executor(dataset_id)
        from app.answers import label
        caps = manifest.get("capabilities")
        abilities = (" 可查指标：" + "、".join(label(m) for m in caps["metrics"]) +
                     "。扩展所需数据：" + "；".join(dict.fromkeys(caps["missing_inputs"].values()))) if caps else ""
        return gr.Dropdown(choices=[(label, key) for key, label in datasets.items()], value=dataset_id), False, f"已导入“{manifest['name']}”，共 {len(manifest['row_counts'])} 张表、{manifest['normalized_row_counts']['orders']} 笔订单。" + abilities

    def dataset_scope(dataset_id):
        if dataset_id not in executors:
            return gr.CheckboxGroup(choices=[], value=[]), "", ""
        manifest = executors[dataset_id].manifest
        return gr.CheckboxGroup(choices=[(status_labels.get(s, s), s) for s in sorted(set(manifest["entities"]["order_statuses"]) | {"delivered"})], value=["delivered"]), manifest["coverage"]["start"], manifest["coverage"]["end"]

    def find_history(query, start, end, dataset_id):
        from datetime import date
        hits = store.search(query, executors[dataset_id].manifest, date.fromisoformat(start) if start else None, date.fromisoformat(end) if end else None)
        table = [[index, hit["created_at"][:16], hit["question"]] for index, hit in enumerate(hits, 1)]
        return hits, gr.Dataframe(value={"headers": ["序号", "时间", "问题"], "data": table}), "\n\n".join(f"**{hit['question']}**\n\n{hit['answer'][:2500]}" for hit in hits) or "没有找到相关分析，试试其他关键词。"

    def choose_history(hits, dataset_id, evt: gr.SelectData):
        selected = hits[int(evt.row_value[0])-1]
        try:
            record = store.turn_evidence(selected["session_id"], selected["turn_index"], executors[dataset_id].manifest)
        except ValueError as error:
            gr.Warning(str(error))
            record = None
        steps = [(f"步骤 {i} · {step['question']}", i) for i, step in enumerate(record.get("steps", []), 1)
                 if step.get("record_id")] if record else []
        return (archives(dataset_id, selected["session_id"]), record,
                gr.Dropdown(choices=[("整轮证据", 0), *steps], value=0, visible=bool(steps)),
                *view_history(record, 0))

    def view_history(record, step):
        if not record:
            return ("该回合没有可打开的独立证据，可在上方继续该会话查看原回答。",
                    gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False))
        selected = record["steps"][step-1] if step else record
        view = present(selected)
        from app.business_answers import scope_text
        text = f"**历史回合 · {selected['created_at'][:16]}**\n\n{selected['question']}"
        shown = display_evidence(selected)
        if shown.get("type") == "query":
            text += "\n\n" + scope_text(AnalysisPlan.model_validate(shown["plan"]))
        return (text, gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=bool(view.headers)),
                gr.DownloadButton(value=view.evidence_file, visible=bool(view.evidence_file)))

    def clear_history_view():
        return None, gr.Dropdown(visible=False, choices=[], value=None), "", gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False)

    def apply_scope(session, dataset_id, date_field, statuses, start, end, order_conditions, item_conditions):
        from app.models import PlanUpdate, Predicate, TimeRange
        session = current_session(session, dataset_id)
        changes = [PlanUpdate(operation="set", field="time_field", values=[date_field]),
                   PlanUpdate(operation="set", field="order_statuses", values=statuses),
                   PlanUpdate(operation="set", field="time_range", period=TimeRange(start=start, end=end))]
        for field, raw in [("order_where", order_conditions), ("item_where", item_conditions)]:
            if raw.strip():
                changes.append(PlanUpdate(operation="set", field=field, predicate=Predicate.model_validate_json(raw)))
        turn = agent.update_conditions(session, changes)
        session.remember("应用范围与条件", turn.text)
        store.save(session, executors[dataset_id].manifest)
        return session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(dataset_id, session.session_id)

    status_labels = STATUS_LABELS
    with gr.Blocks(title="经营数据分析") as demo:
        gr.Markdown("# 经营数据分析\n选择数据，用一句话了解销售、订单、履约和客户表现。")
        with gr.Row():
            dataset = gr.Dropdown(choices=[(label, key) for key, label in datasets.items()], value=initial_dataset, label="分析数据")
        empty_notice = gr.Markdown("还没有可分析的数据。请在下方上传 Excel 或 CSV 文件，核对字段后开始分析。", visible=initial_dataset is None)
        with gr.Column(visible=initial_dataset is not None) as analysis_workspace:
            practice = gr.Checkbox(value=False, visible=False)
            session = gr.State(None)
            with gr.Accordion("历史会话", open=False):
                with gr.Row():
                    saved = gr.Dropdown(choices=archives(initial_dataset).choices, label="选择会话", scale=5)
                    refresh = gr.Button("刷新列表", scale=1)
                    restore = gr.Button("继续分析", scale=1)
            chat = gr.Chatbot(label="分析对话", height=480)
            slot_choice = gr.Dropdown(choices=[], value=None, label="选择补充条件后点击分析", visible=False)
            questions = gr.Dropdown(choices=example_questions(initial_manifest) if initial_manifest else [], label="试试这样问", value=None)
            with gr.Row():
                message = gr.Textbox(label="问题", placeholder="例如：按月份查看商品销售额和订单量；新版交付后输入“数据更新了”", scale=6)
                send = gr.Button("分析", variant="primary", scale=1)
                reset = gr.Button("新建会话", scale=1)
            with gr.Row(visible=False) as drill_controls:
                drill_target = gr.Dropdown(choices=[(label, key) for key, label in TARGETS.items()], value="auto", label="点击结果查看明细", info="选择要进一步查看的内容，再点击表格中的记录。")
                drill_back = gr.Button("返回上层分析")
            active_scope = gr.Markdown()
            result_table = gr.Dataframe(label="本轮查询结果", interactive=False, visible=False, datatype="str")
            bar = gr.BarPlot(x="分组", y="数值", visible=False, height=300)
            line = gr.LinePlot(x="分组", y="数值", visible=False, height=300)
            with gr.Accordion("图表分析", open=False, visible=False) as chart_controls:
                chart_metrics = gr.CheckboxGroup(choices=[], label="选择展示指标")
                interactive_charts = gr.HTML(value="", js_on_load=CLICK_SCRIPT)
            with gr.Row(visible=False) as workflow_controls:
                step_number = gr.Number(value=1, precision=0, minimum=1, maximum=6, label="分析步骤")
                show_step = gr.Button("查看此步骤结果")
                resume_tasks = gr.Button("继续未完成任务")
            retry_query = gr.Button("重试本次查询", visible=False)
            with gr.Row(visible=False) as pagination_controls:
                page_size = gr.Number(value=MAX_ROWS, precision=0, minimum=1, maximum=MAX_ROWS, label="每页行数")
                previous_page = gr.Button("上一页")
                next_page = gr.Button("下一页")
                refresh_page = gr.Button("应用页长并返回首页")
            page_status = gr.Markdown()
            with gr.Row():
                download_csv = gr.DownloadButton("下载结果 CSV", visible=False)
                download_json = gr.DownloadButton("下载完整依据 JSON", visible=False)
                export_all = gr.Button("导出全部结果", visible=False)
                download_all = gr.DownloadButton("下载完整结果", visible=False)
                inspect_quality_button = gr.Button("查看质量依据")
            export_status = gr.Markdown()
            quality_summary = gr.Markdown()
            quality_table = gr.Dataframe(label="分析期间的三类问题（预览最多500条，点击记录定位来源）", interactive=False, visible=False)
            quality_download = gr.DownloadButton("下载当前页质量记录", visible=False)
            quality_export = gr.Button("导出全部质量记录", visible=False)
            quality_full = gr.DownloadButton("下载全部质量记录", visible=False)
            with gr.Accordion("问题原始记录", open=False, visible=False) as quality_source_panel:
                quality_source = gr.JSON()
            evidence = gr.JSON(visible=False)
            add_review_panel(gr, agent, datasets, session, evidence)
            with gr.Accordion("高级分析 · SQL", open=False):
                gr.Markdown("使用 SQL 进一步分析数据。查询上次分析结果时，数据表名为 `analysis_result`。")
                sql_scope = gr.Radio(choices=[("上次成功分析结果（含完整度）", "analysis_result"), ("规范化业务表", "raw")], value="analysis_result", label="SQL 数据范围")
                inspect_sql = gr.Button("查看当前可用字段")
                sql_catalog = gr.JSON(visible=False)
                sql_fields = gr.Dataframe(label="可用字段", interactive=False, visible=False)
                sql_question = gr.Textbox(label="用自然语言描述 SQL", placeholder="例如：计算各月销售额占全期的比例")
                generate = gr.Button("生成 SQL 草稿")
                sql_note = gr.Markdown()
                sql_editor = gr.Code(label="查询语句", language="sql", lines=10, value="SELECT * FROM analysis_result")
                with gr.Accordion("查询参数（可选）", open=False):
                    sql_parameters = gr.Textbox(label="参数值", info='仅在语句包含 ? 占位符时填写，例如 [2025, "delivered"]。', value="[]")
                execute_sql = gr.Button("执行 SQL")
                verify_sql_button = gr.Button("与上次分析核对结果")
                sql_validation = gr.JSON(visible=False)
                sql_validation_message = gr.Markdown()
            with gr.Accordion("调整分析范围", open=False):
                date_field = gr.Dropdown(choices=[("下单时间", "purchase_at"), ("支付确认时间", "approved_at"), ("发货时间", "shipped_at"), ("送达时间", "delivered_at"), ("预计送达时间", "estimated_delivery_at")], value="purchase_at", label="按哪个日期统计")
                statuses = gr.CheckboxGroup(choices=[(status_labels.get(s, s), s) for s in initial_manifest["entities"]["order_statuses"]] if initial_manifest else [], value=["delivered"] if initial_manifest else [], label="订单状态")
                with gr.Row():
                    range_start = gr.Textbox(value=initial_manifest["coverage"]["start"] if initial_manifest else "", label="开始日期（含）")
                    range_end = gr.Textbox(value=initial_manifest["coverage"]["end"] if initial_manifest else "", label="结束日期（不含）")
                gr.Markdown("更多筛选条件可直接在对话中说明，例如“只看广东省的订单”。")
                order_conditions = gr.Code(value="", language="json", visible=False)
                item_conditions = gr.Code(value="", language="json", visible=False)
                apply_conditions = gr.Button("对上次分析应用这些条件")
                condition_field = gr.Dropdown(choices=[("品类", "category_id"), ("商家", "seller_id"), ("客户省份", "customer_state"), ("整单商品金额", "order_sales"), ("支付额", "payment_amount"), ("商品单价", "price")], value="category_id", label="撤回筛选条件")
                remove_filter = gr.Button("撤回此字段全部简单筛选")
            with gr.Accordion("换数据重算", open=False):
                replay_target = gr.Dropdown(choices=[(name, key) for key, name in datasets.items()], label="目标数据快照")
                replay_button = gr.Button("在目标快照新建会话并重算")
                comparison_mode = gr.Radio(choices=[("固定原范围比较", "fixed_scope"), ("按原意图重算比较", "intent_replay")], value="fixed_scope", label="版本比较方式")
                comparison_source = gr.Radio(choices=[("当前选定的成功结果", "current_result"), ("当前分析意图（含尚未成功的条件）", "working_plan")], value="current_result", label="比较哪份分析")
                gr.Markdown("固定范围保留原日期和对象；相同筛选下订单仍可能进入或退出范围。按意图重算工作流时，比较当前选定步骤，前置步骤会重新选择对象。")
                with gr.Row():
                    compare_button = gr.Button("比较两个版本")
                    resume_comparison = gr.Button("继续上次版本比较")
                comparison_summary = gr.Markdown()
                comparison_table = gr.Dataframe(label="版本指标与范围变化（全候选集，预览最多500行）", interactive=False, visible=False, datatype="str")
                comparison_changes = gr.Dataframe(label="业务键与质量差异（预览最多500行）", interactive=False, visible=False, datatype="str")
                comparison_bundle = gr.DownloadButton("下载完整比较证据包", visible=False)
                comparison_report = gr.DownloadButton("下载比较进度及报告", visible=False)
                comparison_json = gr.JSON(visible=False)
            with gr.Accordion("检索历史分析", open=False):
                history_hits = gr.State([])
                history_query = gr.Textbox(label="关键词或指标", placeholder="例如：销售额、回头客、商家延迟率")
                with gr.Row():
                    history_start = gr.Textbox(label="起始日期（可留空）")
                    history_end = gr.Textbox(label="截止日期（不含，可留空）")
                    history_search = gr.Button("检索历史")
                history_table = gr.Dataframe(interactive=False, label="点击记录查看对应回合的证据")
                history_text = gr.Markdown()
                history_record = gr.State(None)
                history_step = gr.Dropdown(choices=[], visible=False, label="历史证据步骤")
                history_evidence_scope = gr.Markdown()
                history_evidence_table = gr.Dataframe(interactive=False, visible=False, datatype="str", label="历史查询结果")
                history_evidence_download = gr.DownloadButton("下载历史回合依据 JSON", visible=False)
        with gr.Accordion("接入新的Excel/CSV数据", open=initial_dataset is None):
            template = gr.Radio(choices=[("完整九表", "full"), ("订单和商品明细", "orders_items")], value="full", label="接入模板")
            amount_mode = gr.Radio(choices=[("行商品额，不含运费", "line"), ("商品单价乘数量", "unit"), ("待确认，仅核对订单", "unknown")], value="line", label="轻量模板金额口径（人民币元）")
            status_map = gr.Textbox(value="{}", label='轻量模板状态映射，如 {"已送达":"delivered"}')
            gr.Markdown("上传包含订单、客户、商品明细、支付、评价、商品、商家、品类和地理信息的 Excel 或 CSV 文件。先识别文件，再核对字段并导入。新导入按正式复赛金额规则计算：同一明细键出现不同非空商品价时，整单商品额和运费暂不计入，订单仍保留；原始记录完整留存。")
            upload_files = gr.File(file_count="multiple", file_types=[".csv", ".xlsx"], type="filepath", label="数据文件")
            inspect_files = gr.Button("识别文件")
            upload_structure = gr.JSON(label="识别结果和前三行样本", visible=True)
            upload_mapping = gr.Code(language="json", visible=False)
            upload_fields = gr.Dataframe(label="核对字段对应关系（可修改来源文件、工作表和来源列名）", interactive=True, visible=False, datatype=["str"]*6, column_count=6, static_columns=[0, 1, 2])
            upload_name = gr.Textbox(label="数据集名称")
            upload_parent = gr.Dropdown(choices=[(name, key) for key, name in datasets.items()], value=None, label="来源版本（可留空；不按上传顺序推断）")
            upload_note = gr.Textbox(value="", label="本次数据变化说明（可留空）")
            upload_origin = gr.Radio(choices=[("自行上传", "uploaded"), ("正式复赛材料", "official"), ("练习数据", "practice"), ("本地构造", "constructed")], value="uploaded", label="材料来源声明")
            import_files = gr.Button("校验映射并导入")
            upload_status = gr.Markdown()
        questions.input(lambda value: value, questions, message)
        slot_choice.input(lambda value: value, slot_choice, message)
        result_outputs = [result_table, bar, line, download_csv, download_json, slot_choice]
        sql_outputs = [sql_editor, sql_parameters, sql_scope, sql_note]
        dataset.change(switch_dataset, [dataset, session], [chat, session, evidence, questions, *result_outputs, saved, *sql_outputs], concurrency_id="analysis")
        shared_outputs = [chat, session, evidence, *result_outputs, saved]
        reset.click(new_session, dataset, [*shared_outputs, *sql_outputs], concurrency_id="analysis")
        refresh.click(archives, dataset, saved)
        restore.click(restore_session, [saved, dataset], [*shared_outputs, *sql_outputs], concurrency_id="analysis")
        inputs = [message, chat, session, dataset, practice]
        outputs = [message, chat, session, evidence, *result_outputs, saved, dataset]
        send.click(submit, inputs, outputs, concurrency_limit=1, concurrency_id="analysis")
        message.submit(submit, inputs, outputs, concurrency_limit=1, concurrency_id="analysis")
        result_table.select(drill, [session, drill_target], shared_outputs, concurrency_limit=1, concurrency_id="analysis")
        drill_back.click(back, session, shared_outputs, concurrency_id="analysis")
        inspect_sql.click(catalog_for_sql, [session, dataset, sql_scope], sql_catalog, concurrency_id="analysis")
        generate.click(draft_sql, [sql_question, session, dataset, sql_scope], [sql_editor, sql_note, session, saved, sql_parameters], concurrency_id="analysis")
        execute_sql.click(run_sql, [sql_editor, sql_parameters, session, dataset, sql_scope], shared_outputs, concurrency_id="analysis")
        evidence.change(refresh_visuals, evidence, [chart_metrics, interactive_charts, download_all, export_status, sql_validation], concurrency_id="analysis")
        from app.business_answers import scope_view
        evidence.change(scope_view, [evidence, session], active_scope, concurrency_id="analysis")
        quality_parent = gr.State(None)
        evidence.change(reset_quality, [evidence, quality_parent], [quality_table, quality_download, quality_summary, quality_source, quality_full, quality_export, quality_source_panel], concurrency_id="analysis")
        evidence.change(result_controls, [evidence, session], [drill_controls, workflow_controls, pagination_controls, export_all, chart_controls, retry_query, page_size, page_status], concurrency_id="analysis")
        sql_catalog.change(catalog_table, sql_catalog, sql_fields, concurrency_id="analysis")
        sql_validation.change(validation_text, sql_validation, sql_validation_message, concurrency_id="analysis")
        upload_mapping.change(mapping_table, upload_mapping, upload_fields, concurrency_id="analysis")
        upload_fields.input(save_mapping, upload_fields, upload_mapping, concurrency_id="analysis")
        chart_metrics.change(render_charts, [evidence, chart_metrics], interactive_charts, concurrency_id="analysis")
        interactive_charts.select(chart_drill, [session, drill_target], shared_outputs, concurrency_id="analysis")
        previous_page.click(lambda s, size: paginate(s, size, -1), [session, page_size], shared_outputs, concurrency_id="analysis")
        next_page.click(lambda s, size: paginate(s, size, 1), [session, page_size], shared_outputs, concurrency_id="analysis")
        refresh_page.click(lambda s, size: paginate(s, size, 0), [session, page_size], shared_outputs, concurrency_id="analysis")
        export_all.click(full_export, session, [download_all, export_status], concurrency_id="analysis")
        inspect_quality_button.click(inspect_quality, [session, evidence], [quality_table, quality_download, quality_summary, quality_export, quality_source_panel, quality_parent], concurrency_id="analysis")
        quality_table.select(inspect_source, session, quality_source, concurrency_id="analysis")
        quality_export.click(lambda s: gr.DownloadButton(value=agent.quality_export(s)["csv_file"], visible=True), session, quality_full, concurrency_id="analysis")
        show_step.click(select_step, [session, step_number], [session, evidence, *result_outputs], concurrency_id="analysis")
        resume_tasks.click(resume_analysis, session, shared_outputs, concurrency_id="analysis")
        retry_query.click(resume_analysis, session, shared_outputs, concurrency_id="analysis")
        remove_filter.click(remove_condition, [session, condition_field], shared_outputs, concurrency_id="analysis")
        replay_button.click(replay_current, [session, replay_target], [*shared_outputs, dataset], concurrency_id="analysis")
        comparison_outputs = [chat, session, comparison_summary, comparison_table, comparison_changes, comparison_bundle, comparison_report, comparison_json, saved]
        compare_button.click(compare_current, [session, replay_target, comparison_mode, comparison_source], comparison_outputs, concurrency_id="analysis")
        resume_comparison.click(lambda s, t, m, source: compare_current(s, t, m, source, True), [session, replay_target, comparison_mode, comparison_source], comparison_outputs, concurrency_id="analysis")
        dataset.change(lambda: ("", gr.Dataframe(visible=False), gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False), gr.DownloadButton(value=None, visible=False), {}),
            outputs=[comparison_summary, comparison_table, comparison_changes, comparison_bundle, comparison_report, comparison_json], concurrency_id="analysis")
        dataset.change(lambda: gr.Dropdown(choices=[(name, key) for key, name in datasets.items()]), outputs=replay_target)
        dataset.change(lambda: gr.Dropdown(choices=[(name, key) for key, name in datasets.items()], value=None), outputs=upload_parent)
        verify_sql_button.click(verify_sql, [sql_editor, sql_parameters, session, dataset, sql_scope], sql_validation, concurrency_id="analysis")
        inspect_files.click(inspect_upload, [upload_files, template], [upload_structure, upload_mapping], concurrency_id="analysis")
        import_files.click(upload_dataset, [upload_files, upload_mapping, upload_name, upload_parent, upload_note, upload_origin, template, amount_mode, status_map], [dataset, practice, upload_status], concurrency_id="analysis")
        history_preview = [history_evidence_scope, history_evidence_table, history_evidence_download]
        history_outputs = [history_record, history_step, *history_preview]
        history_search.click(find_history, [history_query, history_start, history_end, dataset],
                             [history_hits, history_table, history_text], concurrency_id="analysis").success(
                                 clear_history_view, outputs=history_outputs, concurrency_id="analysis")
        history_table.select(choose_history, [history_hits, dataset], [saved, *history_outputs], concurrency_id="analysis")
        history_step.input(view_history, [history_record, history_step], history_preview, concurrency_id="analysis")
        dataset.change(lambda: ([], gr.Dataframe(value=[]), "", *clear_history_view()),
                       outputs=[history_hits, history_table, history_text, *history_outputs], concurrency_id="analysis")
        apply_conditions.click(apply_scope, [session, dataset, date_field, statuses, range_start, range_end, order_conditions, item_conditions], shared_outputs, concurrency_id="analysis")
        dataset.change(dataset_scope, dataset, [statuses, range_start, range_end], concurrency_id="analysis")
        dataset.change(dataset_ready, dataset, [analysis_workspace, empty_notice], concurrency_id="analysis")
    return demo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    import os
    if os.environ.get("ANALYSIS_INSTANCE_DIR"):
        import uvicorn
        from app.team import create_app
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port)
        return
    build_ui().launch(server_name="127.0.0.1", server_port=args.port, inbrowser=False, show_error=True, footer_links=[])


if __name__ == "__main__":
    main()
