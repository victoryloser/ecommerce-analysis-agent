import argparse
import json
import re

from app.config import DATASETS
from app.executor import Executor
from app.orchestrator import AnalysisAgent
from app.presentation import present
from app.answers import STATUS_LABELS
from app.providers import ChatCompletionsProvider
from app.session import Session
from app.models import AnalysisPlan
from app.clarification import choices
from app.drilldown import TARGETS, selected_row_index
from app.session_store import SessionStore
from app.sql_workspace import SQLWorkspace, workspace_catalog
from app.presentation import display_evidence
from app.charts import numeric_columns, render_charts, CLICK_SCRIPT
from app.data_upload import registered_datasets, discover, suggest_mapping, import_upload


def example_questions(manifest):
    """示例问题自行写明年份，不依赖用户看不到的演示配置。"""
    year = manifest["practice"].get("year_context")
    questions = []
    for item in manifest["practice"]["questions"]:
        question = item["question"]
        if year and "年" not in question:
            question = re.sub(r"(?<!\d)(\d{1,2})月", rf"{year}年\1月", question)
        questions.append(question)
    return questions or ["全部数据中，已送达订单的商品销售额是多少？", "按月份查看商品销售额和订单量", "按品类查看商品销售额", "查看延迟率最高的商家"]


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
        frame = pd.DataFrame(view.chart, columns=["分组", "数值"])
        return (
            gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=bool(view.headers)),
            gr.BarPlot(value=frame, x="分组", y="数值", y_title=view.chart_title, title=view.chart_title, visible=bool(view.chart) and view.chart_kind == "bar", sort="-y"),
            gr.LinePlot(value=frame, x="分组", y="数值", y_title=view.chart_title, title=view.chart_title, visible=bool(view.chart) and view.chart_kind == "line", sort="x"),
            gr.DownloadButton(value=view.csv_file, visible=view.csv_file is not None),
            gr.DownloadButton(value=view.evidence_file, visible=False),
            gr.Dropdown(choices=options, value=None, visible=bool(options)),
        )

    def submit(question, history, session, dataset_id, practice_context):
        session = current_session(session if session and session.dataset_id == dataset_id else None, dataset_id)
        provider()
        turn = agent.respond(session, question, practice_context)
        return "", session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(dataset_id, session.session_id)

    def switch_dataset(dataset_id, session):
        questions = example_questions(executors[dataset_id].manifest) if dataset_id in executors else []
        if session and session.dataset_id == dataset_id and session.last_evidence:
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
        return workspace_catalog(current_session(session, dataset_id), scope)

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
        workflow = session.workflow_evidence if session else {}
        return (gr.Row(visible=can_drill), gr.Row(visible=workflow.get("type") == "workflow"),
                gr.Row(visible=ready and (result.get("truncated", False) or result.get("row_offset", 0) > 0 or len(result.get("rows", [])) >= 100)),
                gr.Button(visible=ready), gr.Accordion(visible=bool(numeric_columns(evidence))))

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
            rows.extend([table, field, TABLE_GRAINS[table] + " · " + FIELD_MEANINGS[field], config["file"], config["sheet"] or "", source]
                        for field, source in config["columns"].items())
        return gr.Dataframe(value={"headers": ["业务表", "标准字段", "含义", "来源文件", "工作表（CSV留空）", "来源列名"], "data": rows}, visible=True)

    def save_mapping(frame):
        mapping = {"tables": {}}
        for table, field, _, filename, sheet, source in frame.itertuples(index=False, name=None):
            config = mapping["tables"].setdefault(table, {"file": filename, "sheet": sheet or None, "columns": {}})
            if config["file"] != filename or config["sheet"] != (sheet or None):
                raise gr.Error("同一业务表的字段需来自同一文件和工作表。")
            config["columns"][field] = source
        return json.dumps(mapping, ensure_ascii=False)

    def select_step(session, index):
        from app.models import QueryResult
        root = session.workflow_evidence
        if root.get("type") != "workflow":
            raise ValueError("当前不是多步骤分析")
        selected = root["steps"][int(index)-1]
        session.focus_step = int(index)
        if selected.get("type") == "query" and selected["result"]["status"] in {"ok", "empty"}:
            session.last_successful_plan = AnalysisPlan.model_validate(selected["plan"])
            session.last_result = QueryResult.model_validate(selected["result"])
            session.analysis_evidence = selected
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
                  (f"共同变化 {r['membership']['common_change']}；进入 {r['membership']['entered_value']}；退出 {r['membership']['exited_value']}；未确认 {r['membership']['unknown_count']}" if r["membership"] else "按指标定义重算"),
                  "通过" if r["reconciled"] is True else "暂不能确认" if r["reconciled"] is None else "不一致",
                  "是" if r["displayed_before"] else "否", "是" if r["displayed_after"] else "否"]
                 for r in report.get("metric_comparisons", [])[:500]]
        changes = [[r["layer"], r["key"], r["change"], r["before"], r["after"], r["explanation"]] for r in report.get("preview", [])]
        stringify = lambda rows: [["—" if v is None else json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v) for v in row] for row in rows]
        return (session.transcript, session, turn.text,
            gr.Dataframe(value={"headers": ["期间", "分组", "指标", "原值", "原完整度", "新值", "新完整度", "差额", "范围成员分解", "勾稽", "原表可见", "新表可见"], "data": stringify(table)}, visible=bool(table)),
            gr.Dataframe(value={"headers": ["差异层次", "业务键", "变化", "原记录", "新记录", "解释"], "data": stringify(changes)}, visible=bool(changes)),
            gr.DownloadButton(value=state.get("bundle_file"), visible=state.get("status") == "completed"),
            gr.DownloadButton(value=state["report_file"], visible=True), state, archives(session.dataset_id, session.session_id))

    def remove_condition(session, field):
        from app.models import PlanUpdate
        turn = agent.update_conditions(session, PlanUpdate(operation="remove", field=field))
        session.remember("撤回条件：" + field, turn.text)
        store.save(session, executors[session.dataset_id].manifest)
        return session.transcript, session, turn.evidence, *result_components(turn.evidence), archives(session.dataset_id, session.session_id)

    def inspect_quality(session):
        turn = agent.quality_details(session)
        view = present(turn.evidence)
        return gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=True), gr.DownloadButton(value=view.csv_file, visible=True)

    def chart_drill(session, target, evt: gr.SelectData):
        current = display_evidence(session.last_evidence)
        if current.get("type") != "query":
            raise ValueError("请选择结构化分析图表")
        result = current["result"]
        ids = [str(row[result["columns"].index("entity_id")]) for row in result["rows"]] if "entity_id" in result["columns"] else ["all"]
        row = ids.index(str(evt.value))
        agent.drill(session, row, target)
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def page_previous(session, size):
        agent.page(session, -1, int(size))
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def page_next(session, size):
        agent.page(session, 1, int(size))
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def page_refresh(session, size):
        agent.page(session, 0, int(size))
        return session.transcript, session, session.last_evidence, *result_components(session.last_evidence), archives(session.dataset_id, session.session_id)

    def full_export(session):
        record = agent.export(session)
        return gr.DownloadButton(value=record["csv_file"], visible=True), f"已生成完整结果，共 {record['row_count']} 行，可点击下载。"

    def verify_sql(sql, parameters, session, dataset_id, scope):
        return workspace.verify(current_session(session, dataset_id), sql, json.loads(parameters), scope)

    def inspect_upload(files):
        structure = discover(files)
        return structure, json.dumps(suggest_mapping(structure), ensure_ascii=False, indent=2)

    def upload_dataset(files, mapping, name, parent=None, note="", origin="uploaded"):
        version = {"parent_dataset_id": parent, "change_note": note, "data_origin": origin} if parent or note or origin != "uploaded" else {}
        manifest = import_upload(files, json.loads(mapping), name, **version)
        dataset_id = manifest["dataset_id"]
        datasets[dataset_id] = manifest["name"]
        executors[dataset_id] = Executor(dataset_id)
        return gr.Dropdown(choices=[(label, key) for key, label in datasets.items()], value=dataset_id), False, f"已导入“{manifest['name']}”，共 {len(manifest['row_counts'])} 张表、{manifest['row_counts'].get('orders', 0)} 笔订单。"

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
        return archives(dataset_id, selected["session_id"])

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
                message = gr.Textbox(label="问题", placeholder="例如：按月份查看商品销售额和订单量", scale=6)
                send = gr.Button("分析", variant="primary", scale=1)
                reset = gr.Button("新建会话", scale=1)
            with gr.Row(visible=False) as drill_controls:
                drill_target = gr.Dropdown(choices=[(label, key) for key, label in TARGETS.items()], value="auto", label="点击结果查看明细", info="选择要进一步查看的内容，再点击表格中的记录。")
                drill_back = gr.Button("返回上层分析")
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
            with gr.Row(visible=False) as pagination_controls:
                page_size = gr.Number(value=100, precision=0, minimum=1, maximum=500, label="每页行数")
                previous_page = gr.Button("上一页")
                next_page = gr.Button("下一页")
                refresh_page = gr.Button("更新显示行数")
            with gr.Row():
                download_csv = gr.DownloadButton("下载结果 CSV", visible=False)
                download_json = gr.DownloadButton("下载完整依据 JSON", visible=False)
                export_all = gr.Button("导出全部结果", visible=False)
                download_all = gr.DownloadButton("下载完整结果", visible=False)
                inspect_quality_button = gr.Button("查看质量依据")
            export_status = gr.Markdown()
            quality_table = gr.Dataframe(label="当前分析范围质量记录（最多500条，来源行号从表头下一行起）", interactive=False, visible=False)
            quality_download = gr.DownloadButton("下载质量记录", visible=False)
            evidence = gr.JSON(visible=False)
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
                history_table = gr.Dataframe(interactive=False, label="点击记录后可在上方恢复对应会话")
                history_text = gr.Markdown()
        with gr.Accordion("接入新的Excel/CSV数据", open=initial_dataset is None):
            gr.Markdown("上传包含订单、客户、商品明细、支付、评价、商品、商家、品类和地理信息的 Excel 或 CSV 文件。先识别文件，再核对字段并导入。")
            upload_files = gr.File(file_count="multiple", file_types=[".csv", ".xlsx"], type="filepath", label="数据文件")
            inspect_files = gr.Button("识别文件")
            upload_structure = gr.JSON(visible=False)
            upload_mapping = gr.Code(language="json", visible=False)
            upload_fields = gr.Dataframe(label="核对字段对应关系（可修改来源文件、工作表和来源列名）", interactive=True, visible=False, datatype=["str"]*6, column_count=6, static_columns=[0, 1, 2])
            upload_name = gr.Textbox(label="数据集名称")
            upload_parent = gr.Dropdown(choices=[(name, key) for key, name in datasets.items()], value=None, label="来源版本（可留空；不按上传顺序推断）")
            upload_note = gr.Textbox(value="", label="本次数据变化说明（可留空）")
            upload_origin = gr.Radio(choices=[("自行上传", "uploaded"), ("正式复赛材料", "official"), ("练习数据", "practice"), ("本地构造", "constructed")], value="uploaded", label="材料来源声明")
            import_files = gr.Button("校验映射并导入")
            upload_status = gr.Markdown()
        questions.change(lambda value: value, questions, message)
        slot_choice.change(lambda value: value, slot_choice, message)
        result_outputs = [result_table, bar, line, download_csv, download_json, slot_choice]
        sql_outputs = [sql_editor, sql_parameters, sql_scope, sql_note]
        dataset.change(switch_dataset, [dataset, session], [chat, session, evidence, questions, *result_outputs, saved, *sql_outputs], concurrency_id="analysis")
        shared_outputs = [chat, session, evidence, *result_outputs, saved]
        reset.click(new_session, dataset, [*shared_outputs, *sql_outputs], concurrency_id="analysis")
        refresh.click(archives, dataset, saved)
        restore.click(restore_session, [saved, dataset], [*shared_outputs, *sql_outputs], concurrency_id="analysis")
        inputs = [message, chat, session, dataset, practice]
        outputs = [message, chat, session, evidence, *result_outputs, saved]
        send.click(submit, inputs, outputs, concurrency_limit=1, concurrency_id="analysis")
        message.submit(submit, inputs, outputs, concurrency_limit=1, concurrency_id="analysis")
        result_table.select(drill, [session, drill_target], shared_outputs, concurrency_limit=1, concurrency_id="analysis")
        drill_back.click(back, session, shared_outputs, concurrency_id="analysis")
        inspect_sql.click(catalog_for_sql, [session, dataset, sql_scope], sql_catalog, concurrency_id="analysis")
        generate.click(draft_sql, [sql_question, session, dataset, sql_scope], [sql_editor, sql_note, session, saved, sql_parameters], concurrency_id="analysis")
        execute_sql.click(run_sql, [sql_editor, sql_parameters, session, dataset, sql_scope], shared_outputs, concurrency_id="analysis")
        evidence.change(refresh_visuals, evidence, [chart_metrics, interactive_charts, download_all, export_status, sql_validation], concurrency_id="analysis")
        evidence.change(lambda: (gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False)), outputs=[quality_table, quality_download], concurrency_id="analysis")
        evidence.change(result_controls, [evidence, session], [drill_controls, workflow_controls, pagination_controls, export_all, chart_controls], concurrency_id="analysis")
        sql_catalog.change(catalog_table, sql_catalog, sql_fields, concurrency_id="analysis")
        sql_validation.change(validation_text, sql_validation, sql_validation_message, concurrency_id="analysis")
        upload_mapping.change(mapping_table, upload_mapping, upload_fields, concurrency_id="analysis")
        upload_fields.input(save_mapping, upload_fields, upload_mapping, concurrency_id="analysis")
        chart_metrics.change(render_charts, [evidence, chart_metrics], interactive_charts, concurrency_id="analysis")
        interactive_charts.select(chart_drill, [session, drill_target], shared_outputs, concurrency_id="analysis")
        previous_page.click(page_previous, [session, page_size], shared_outputs, concurrency_id="analysis")
        next_page.click(page_next, [session, page_size], shared_outputs, concurrency_id="analysis")
        refresh_page.click(page_refresh, [session, page_size], shared_outputs, concurrency_id="analysis")
        export_all.click(full_export, session, [download_all, export_status], concurrency_id="analysis")
        inspect_quality_button.click(inspect_quality, session, [quality_table, quality_download], concurrency_id="analysis")
        show_step.click(select_step, [session, step_number], [session, evidence, *result_outputs], concurrency_id="analysis")
        resume_tasks.click(lambda session: (agent.respond(session, "/continue"), session)[1], session, session, concurrency_id="analysis").then(
            lambda s: (s.transcript, s, s.last_evidence, *result_components(s.last_evidence), archives(s.dataset_id, s.session_id)), session, shared_outputs, concurrency_id="analysis")
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
        inspect_files.click(inspect_upload, upload_files, [upload_structure, upload_mapping], concurrency_id="analysis")
        import_files.click(upload_dataset, [upload_files, upload_mapping, upload_name, upload_parent, upload_note, upload_origin], [dataset, practice, upload_status], concurrency_id="analysis")
        history_search.click(find_history, [history_query, history_start, history_end, dataset], [history_hits, history_table, history_text], concurrency_id="analysis")
        history_table.select(choose_history, [history_hits, dataset], saved, concurrency_id="analysis")
        apply_conditions.click(apply_scope, [session, dataset, date_field, statuses, range_start, range_end, order_conditions, item_conditions], shared_outputs, concurrency_id="analysis")
        dataset.change(dataset_scope, dataset, [statuses, range_start, range_end], concurrency_id="analysis")
        dataset.change(dataset_ready, dataset, [analysis_workspace, empty_notice], concurrency_id="analysis")
    return demo


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    build_ui().launch(server_name="127.0.0.1", server_port=args.port, inbrowser=False, show_error=True, footer_links=[])


if __name__ == "__main__":
    main()
