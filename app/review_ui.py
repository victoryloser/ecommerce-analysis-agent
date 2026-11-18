"""Gradio view for persisted business reviews and their independent evidence."""
from decimal import Decimal

from app.answers import cell, plan_label
from app.business_answers import scope_text
from app.models import AnalysisPlan
from app.presentation import display_evidence, present
from app.review import (METRICS, REVIEW_STEPS, build_review, compare_review, load_review, review_base,
                        review_drill, review_record, review_sources)
from app.semantic import PlanError
from app.session import Session


CHANGES = {"eligibility_entered": "金额资格恢复", "eligibility_exited": "金额资格退出", "scope_uncertain": "范围归属待确认"}


def overview_text(record, manifest):
    plan = AnalysisPlan.model_validate(record["plan"])
    result = record["result"]
    scope = f"**{manifest['name']}** · {scope_text(plan)}"
    if not result["rows"]:
        return scope + "\n\n两个期间没有可展示的汇总结果。"
    values = dict(zip(result["columns"], result["rows"][0]))
    ratios = result["ratios"][0] if result["ratios"] else {}
    rows = ["| 指标 | 基期 | 本期 | 差额 | 变化率 |", "| --- | ---: | ---: | ---: | ---: |"]
    for metric in METRICS:
        numbers = [cell(column, values[column], plan, ratios) for column in (f"{metric}_{s}" for s in ("previous", "current", "change", "rate"))]
        rows.append("| " + " | ".join([plan_label(metric, plan), *numbers]) + " |")
    return scope + "\n\n" + "\n".join(rows)


def contribution_text(record):
    result = record["result"]
    if result["status"] not in {"ok", "empty"}:
        return "品类贡献未完成。" + (result["error"] or "请重新生成复盘。")
    rows = [dict(zip(result["columns"], row)) for row in result["rows"]]
    if not rows:
        return "所选期间没有品类贡献记录。"
    plan = AnalysisPlan.model_validate(record["plan"])
    net = rows[0]["total_change"]
    lines = [f"**商品额净差额 {cell('total_change', net, plan)} 元**" if net is not None else "部分金额尚未确认，暂无法计算整体商品额净差额。"]
    if net is not None and Decimal(net) == 0:
        lines.append("净差额为零，贡献率未定义。已确认的各品类差额仍可查看。")
    if result["truncated"]:
        lines.append("表格为当前页。可导出全部品类查看完整结果。")
    else:
        known = [(Decimal(row["sales_change"]), row["entity_name"]) for row in rows if row["sales_change"] is not None]
        for name, subset in (("主要拉动", sorted((r for r in known if r[0] > 0), key=lambda r: -r[0])),
                             ("主要拖累", sorted((r for r in known if r[0] < 0), key=lambda r: r[0]))):
            if subset:
                lines.append(f"**{name}** " + "、".join(f"{label} {amount:+,.2f} 元" for amount, label in subset[:3]))
    return "\n\n".join(lines)


def version_view(version, plan):
    columns = ["期间", "订单", "影响", "商品额（元）", "运费（元）", "变动异常明细键"]
    if not version:
        return "选择目标快照后，可比较同一范围的两期结果。", columns, [], []
    if version["status"] != "completed":
        return "版本比较尚未完成。" + "\n\n".join(version["errors"]), columns, [], []
    if version["contract_changes"] or version["scope_changes"]:
        return "两套快照的计算口径或生效范围不同，可下载比较报告查看两侧结果。", columns, [], []
    lines = [f"**目标快照 {version['target_label']}**", "",
             "| 指标 | 原版基期 | 原版本期 | 新版基期 | 新版本期 | 对两期差额的影响 |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for item in sorted(version["reconciliation"], key=lambda r: METRICS.index(r["metric"])):
        values = [*item["values"], item["revision_effect"]]
        numbers = [cell(item["metric"], value, plan) if value is not None else "待确认" for value in values]
        lines.append("| " + " | ".join([plan_label(item["metric"], plan), *numbers]) + " |")
    lines.append("\n差额影响 = 新版两期差额减去原版两期差额。")
    for item in version["metric_comparisons"]:
        if item["metric"] != "sales" or not item["membership"]:
            continue
        movement = item["membership"]
        values = [Decimal(movement[key]) for key in ("eligibility_entered_value", "eligibility_exited_value", "scope_uncertain_change")]
        if any(values):
            period = "本期" if item["period"] == "current" else "基期"
            lines.append(f"\n{period}商品额 · 资格恢复 {values[0]:,.2f} 元 · 资格退出 {values[1]:,.2f} 元 · 归属待确认差额 {values[2]:,.2f} 元")
    rows, choices = [], []
    for i, row in enumerate(version["affected_orders"]):
        period = "本期" if row["period"] == "current" else "基期"
        change = CHANGES[row["change"]]
        choices.append((f"{period} · {row['order_id']} · {change}", i))
        issue_keys = "、".join(":".join(map(str, issue["item_key"])) for issue in row["issues"])
        amounts = [cell(m, row["amounts"][m], plan) if m in row["amounts"] else "—" for m in ("sales", "freight")]
        rows.append([period, row["order_id"], change, *amounts, issue_keys or "—"])
    if not rows:
        lines.append("\n该比较没有金额资格或归属变化的订单。")
    elif version["affected_count"] > len(rows):
        lines.append(f"\n共 {version['affected_count']} 条期间订单影响，表格展示前 {len(rows)} 条，完整记录见比较包。")
    return "\n".join(lines), columns, rows, choices


def add_review_panel(gr, agent, datasets, session_state, active_evidence):
    def saved_choices(session, value=None):
        return gr.Dropdown(choices=[(title, key) for key, title in reversed(list(session.reviews.items()))] if session else [], value=value)

    def sync(session, evidence, selected):
        available = session is not None and selected in session.reviews
        return (saved_choices(session, selected if available else None),
                gr.Button(interactive=bool(session and review_base(display_evidence(evidence)))),
                gr.skip() if available else gr.Group(visible=False))

    def generate(session, evidence):
        card = build_review(agent, session, display_evidence(evidence))
        return session, saved_choices(session, card["record_id"])

    def recalculate(session, selected, target):
        card = compare_review(agent, session, selected, target)
        return session, saved_choices(session, card["record_id"])

    def render(session, selected):
        if not selected:
            return (gr.Group(visible=False),) + (gr.skip(),) * (len(outputs)-1)
        card = load_review(agent, session, selected)
        overview = review_record(agent, session, card, "overview_id")
        contribution = review_record(agent, session, card, "contribution_id")
        view = present(contribution)
        result = contribution["result"]
        entities = [(str(r[1]), str(r[0])) for r in result["rows"]] if view.headers else []
        plan = AnalysisPlan.model_validate(card["plan"])
        version = card.get("version")
        summary, headers, rows, orders = version_view(version, plan)
        targets = [(name, key) for key, name in datasets.items() if key != session.dataset_id]
        return (gr.Group(visible=True), overview_text(overview, agent.executors[session.dataset_id].manifest),
                contribution_text(contribution), gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=bool(view.headers)),
                gr.Dropdown(choices=entities, value=None), gr.DownloadButton(value=view.csv_file, visible=bool(view.csv_file)),
                gr.Button(visible=result["truncated"]), gr.DownloadButton(value=None, visible=False),
                gr.Dataframe(visible=False), gr.DownloadButton(value=None, visible=False),
                gr.Dropdown(choices=targets, value=version["target_snapshot"]["dataset_id"] if version else None),
                summary, gr.Dataframe(value={"headers": headers, "data": rows}, visible=bool(rows)),
                gr.Dropdown(choices=orders, value=None), "", gr.Dataframe(visible=False), gr.JSON(value=None, visible=False),
                gr.DownloadButton(value=version["report_file"] if version else None, visible=bool(version)),
                gr.DownloadButton(value=version.get("bundle_file") if version else None, visible=bool(version and version.get("bundle_file"))),
                gr.DownloadButton(value=present(card).evidence_file, visible=True), gr.Dropdown(value=REVIEW_STEPS[0][0]),
                gr.DownloadButton(value=present(overview).evidence_file, visible=True))

    def view_sellers(session, selected, category):
        if category is None:
            raise PlanError("请选择要查看的品类")
        view = present(review_drill(agent, session, selected, category))
        return (gr.Dataframe(value={"headers": view.headers, "data": view.table}, visible=True),
                gr.DownloadButton(value=view.evidence_file, visible=bool(view.evidence_file)))

    def drill_row(session, selected, evt: gr.SelectData):
        return view_sellers(session, selected, evt.row_value[0])

    def export_contributions(session, selected):
        record = review_record(agent, session, load_review(agent, session, selected), "contribution_id")
        isolated = Session(session.dataset_id, session_id=session.session_id, last_evidence=record)
        return gr.DownloadButton(value=agent.export(isolated)["csv_file"], visible=True)

    def source_rows(session, selected, order):
        if order is None:
            raise PlanError("请选择要查验的受影响订单")
        evidence = review_sources(agent, session, selected, order)
        records = evidence["records"]
        summary = f"**{evidence['dataset']} · 订单 {evidence['order_id']}**\n\n{evidence['note']}"
        rows = []
        for record in records:
            cells = [record["values"].get(key) for key in ("order_item_id", "price", "freight_value", "payment_value")]
            rows.append([record["table"], record["file"], record["sheet"] or "—", str(record["record_number"]),
                         *["—" if value in (None, "") else str(value) for value in cells]])
        headers = ["业务表", "来源文件", "工作表", "来源行号", "明细序号", "商品价", "运费", "支付额"]
        return (summary, gr.Dataframe(value={"headers": headers, "data": rows}, visible=bool(rows)),
                gr.JSON(value=evidence, visible=True))

    def download_step(session, selected, step):
        record = review_record(agent, session, load_review(agent, session, selected), step)
        return gr.DownloadButton(value=present(record).evidence_file, visible=True)

    with gr.Accordion("经营复盘", open=False):
        gr.Markdown("从两期总体比较生成复盘。打开已保存的复盘时保留原数据和筛选条件。")
        with gr.Row():
            saved = gr.Dropdown(choices=[], label="已保存的复盘", scale=3)
            create = gr.Button("从当前结果生成复盘", variant="primary", interactive=False)
        with gr.Group(visible=False) as body:
            overview = gr.Markdown()
            with gr.Tabs():
                with gr.Tab("品类贡献"):
                    contribution = gr.Markdown()
                    groups = gr.Dataframe(interactive=False, visible=False, datatype="str", label="品类贡献明细")
                    with gr.Row():
                        group = gr.Dropdown(choices=[], label="品类")
                        drill = gr.Button("查看该品类本期商家")
                    sellers = gr.Dataframe(interactive=False, visible=False, datatype="str", label="本期商家")
                    seller_download = gr.DownloadButton("下载商家查询依据", visible=False)
                    csv_download = gr.DownloadButton("下载品类表 CSV", visible=False)
                    export = gr.Button("导出全部品类", visible=False)
                    full_download = gr.DownloadButton("下载全部品类 CSV", visible=False)
                with gr.Tab("换版影响"):
                    with gr.Row():
                        target = gr.Dropdown(choices=[], label="复盘目标快照", scale=3)
                        compare = gr.Button("按同一范围比较")
                    version = gr.Markdown()
                    changes = gr.Dataframe(interactive=False, datatype="str", visible=False, label="受影响订单明细")
                    with gr.Row():
                        order = gr.Dropdown(choices=[], label="受影响订单")
                        inspect = gr.Button("查看订单原始记录")
                    source_summary = gr.Markdown()
                    source_table = gr.Dataframe(interactive=False, datatype="str", visible=False, label="订单来源行")
                    sources = gr.JSON(label="订单原始记录及异常键", visible=False)
                    report = gr.DownloadButton("下载版本比较报告", visible=False)
                    bundle = gr.DownloadButton("下载版本比较证据包", visible=False)
                with gr.Tab("复盘依据"):
                    card_download = gr.DownloadButton("下载复盘记录", visible=False)
                    step = gr.Dropdown(choices=[(title, key) for key, title in REVIEW_STEPS], value=REVIEW_STEPS[0][0], label="证据步骤")
                    step_download = gr.DownloadButton("下载此步查询依据 JSON", visible=False)

    outputs = [body, overview, contribution, groups, group, csv_download, export, full_download, sellers, seller_download,
               target, version, changes, order, source_summary, source_table, sources, report, bundle, card_download, step, step_download]
    active_evidence.change(sync, [session_state, active_evidence, saved], [saved, create, body], concurrency_id="analysis")
    create.click(generate, [session_state, active_evidence], [session_state, saved], concurrency_id="analysis").success(
        render, [session_state, saved], outputs, concurrency_id="analysis")
    saved.input(render, [session_state, saved], outputs, concurrency_id="analysis")
    compare.click(recalculate, [session_state, saved, target], [session_state, saved], concurrency_id="analysis").success(
        render, [session_state, saved], outputs, concurrency_id="analysis")
    drill.click(view_sellers, [session_state, saved, group], [sellers, seller_download], concurrency_id="analysis")
    groups.select(drill_row, [session_state, saved], [sellers, seller_download], concurrency_id="analysis")
    export.click(export_contributions, [session_state, saved], full_download, concurrency_id="analysis")
    inspect.click(source_rows, [session_state, saved, order], [source_summary, source_table, sources], concurrency_id="analysis")
    step.input(download_step, [session_state, saved, step], step_download, concurrency_id="analysis")
