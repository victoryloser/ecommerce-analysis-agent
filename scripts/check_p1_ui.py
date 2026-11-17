"""会话、下钻与 SQL 界面回归：HTTP提交、真实SelectData回调、服务重建后从磁盘恢复。固定Provider。"""
import json


class ScriptedProvider:
    model = "p1-ui-scripted"

    def complete(self, messages):
        from app.demo import practice_plans
        from app.semantic import load_manifest
        context = json.loads(messages[1]["content"])
        if "scope" in context["catalog"]:
            return json.dumps({"sql": "SELECT entity_id, sales, sales / SUM(sales) OVER () AS share FROM analysis_result ORDER BY entity_id",
                               "explanation": "对完整月度分析结果计算占比"})
        plan = practice_plans(load_manifest(context["catalog"]["dataset_id"]))["Q02"]
        return plan.model_copy(update={"dimension": "month"}).model_dump_json()


def main():
    from app.snapshots import resolve_dataset
    dataset = resolve_dataset("csv_small")
    import asyncio
    from pathlib import Path

    import gradio as gr
    from gradio_client import Client

    import app.ui as ui
    from app.presentation import present

    ui.ChatCompletionsProvider = ScriptedProvider
    demo = ui.build_ui()
    try:
        demo.launch(server_name="127.0.0.1", server_port=7863, prevent_thread_lock=True, quiet=True)
        client = Client("http://127.0.0.1:7863/", verbose=False)
        client.predict(dataset, api_name="/switch_dataset")
        monthly = client.predict("全期按月看商品销售额", [], dataset, True, api_name="/submit")
        source = monthly[2]
        assert len(source["result"]["rows"]) == 6
        fn = next(fn for fn in demo.fns.values() if fn.api_name == "drill")
        # 选中视觉第0行，但其实际内容为六月：验证表格排序后仍按ID下钻。
        row = present(source).table[-1]
        event = gr.SelectData(None, {"index": [0, 0], "value": row[0], "row_value": row})
        drilled = asyncio.run(demo.process_api(fn, [None, "auto"], state=demo.state_holder[client.session_hash],
                                              session_hash=client.session_hash, event_data=event))
        drill_evidence = drilled["data"][2].root
        assert drill_evidence["plan"]["time_range"] == {"start": "2025-06-01", "end": "2025-07-01"}
        assert drill_evidence["planning_calls"] == 0
        back = client.predict(api_name="/back")
        assert back[1]["record_id"] == source["record_id"]
        draft = client.predict("计算各月销售占比", dataset, "analysis_result", api_name="/draft_sql")
        assert "OVER" in draft[0]
        explored = client.predict(draft[0], "[]", dataset, "analysis_result", api_name="/run_sql")
        assert explored[1]["type"] == "sql_exploration" and len(explored[1]["result"]["rows"]) == 6
        assert explored[3]["visible"] and not explored[4]["visible"]
        downloaded = json.loads(Path(explored[6]["value"]).read_text(encoding="utf-8"))
        assert downloaded["result_sha256"] == explored[1]["result_sha256"]
        session_id = explored[1]["session_id"]
        transcript = explored[0]
    finally:
        demo.close()

    # 新建服务实例，没有前一服务的gr.State或内存Session。
    demo = ui.build_ui()
    try:
        demo.launch(server_name="127.0.0.1", server_port=7863, prevent_thread_lock=True, quiet=True)
        client = Client("http://127.0.0.1:7863/", verbose=False)
        client.predict(dataset, api_name="/switch_dataset")
        restored = client.predict(session_id, dataset, api_name="/restore_session")
        assert restored[0] == transcript and restored[1]["type"] == "sql_exploration"
        assert restored[-4] == draft[0] and restored[-2] == "analysis_result"
        raw = client.predict("SELECT COUNT(*) AS n FROM orders", "[]", dataset, "raw", api_name="/run_sql")
        assert raw[1]["result"]["rows"] == [[180]]
        print("Workspace UI: stable-ID click drill/back + generated/edited SQL + CSV/JSON + disk restore after service rebuild: PASS (scripted provider)")
    finally:
        demo.close()


if __name__ == "__main__":
    main()
