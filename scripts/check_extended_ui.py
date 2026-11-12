"""新增界面链路：工作流、图表点击、分页完整导出、检索与陌生字段导入。固定Provider。"""
import json


class ScriptedProvider:
    model="extended-ui-scripted"
    def complete(self,messages):
        from app.demo import practice_plans
        from app.semantic import load_manifest
        context=json.loads(messages[1]["content"])
        plans=practice_plans(load_manifest(context["catalog"]["dataset_id"]))
        if context["question"]=="复合分析":
            steps=[plans["Q03"],plans["Q03"].model_copy(update={"intent":"contribution","dimension":"category"}),plans["Q08"].model_copy(update={"explanation_topic":"causality"})]
            plan=plans["Q02"].model_copy(update={"intent":"workflow","steps":steps})
        else:
            plan=plans["Q02"].model_copy(update={"intent":"details","metric_ids":[]})
        return plan.model_dump_json()


def main():
    import asyncio
    import csv
    from pathlib import Path
    import gradio as gr
    from gradio_client import Client, handle_file
    import app.ui as ui
    from app.config import ROOT
    from app.charts import render_charts

    ui.ChatCompletionsProvider=ScriptedProvider
    demo=ui.build_ui()
    try:
        demo.launch(server_name="127.0.0.1",server_port=7864,prevent_thread_lock=True,quiet=True)
        client=Client("http://127.0.0.1:7864/",verbose=False)
        client.predict("csv_small",api_name="/switch_dataset")
        workflow=client.predict("复合分析",[],"csv_small",True,api_name="/submit")
        assert workflow[2]["type"]=="workflow" and len(workflow[2]["steps"])==3
        first=client.predict(1,api_name="/select_step")
        assert first[0]["plan"]["intent"]=="compare"
        third=client.predict(3,api_name="/select_step")
        assert third[0]["type"]=="explain"
        selected=client.predict(2,api_name="/select_step")
        evidence=selected[0]
        assert evidence["plan"]["intent"]=="contribution"
        assert 'data-group-id=' in render_charts(evidence,["sales_change","contribution_rate"])
        fn=next(fn for fn in demo.fns.values() if fn.api_name=="chart_drill")
        key=evidence["result"]["rows"][0][0]
        event=gr.SelectData(None,{"index":0,"value":key,"selected":True})
        clicked=asyncio.run(demo.process_api(fn,[None,"orders"],state=demo.state_holder[client.session_hash],session_hash=client.session_hash,event_data=event))
        assert clicked["data"][2].root["plan"]["filters"][0]["values"]==[key]
        details=client.predict("订单明细",[],"csv_small",True,api_name="/submit")
        page=client.predict(50,api_name="/page_refresh")
        assert len(page[1]["result"]["rows"])==50 and page[1]["result"]["truncated"]
        next_page=client.predict(50,api_name="/page_next")
        assert next_page[1]["result"]["row_offset"]==50
        exported=client.predict(api_name="/full_export")
        with Path(exported[0]["value"]).open(encoding="utf-8-sig",newline="") as handle:
            assert len(list(csv.DictReader(handle)))==159
        checked=client.predict("SELECT * FROM analysis_result", "[]", "csv_small", "analysis_result",api_name="/verify_sql")
        assert checked["matched"] and checked["differing_rows"]==0
        history=client.predict("复合分析","","","csv_small",api_name="/find_history")
        assert "步骤" in history[-1]

        # 从示例 CSV复制数据走上传UI，字段映射能力另由陌生列名回归覆盖。
        files=[handle_file(str(path)) for path in sorted((ROOT/"material/ecommerce_agent_demo_csv").glob("*.csv")) if path.stem!="practice_questions"]
        inspected=client.predict(files,api_name="/inspect_upload")
        assert len(inspected[0])==9
        field_table=client.predict(inspected[1],api_name="/mapping_table")
        editable_mapping=client.predict(field_table["value"],api_name="/save_mapping")
        assert json.loads(editable_mapping)==json.loads(inspected[1])
        imported=client.predict(files,editable_mapping,"UI上传核验",api_name="/upload_dataset")
        assert "UI上传核验" in imported[-1]
        new_id=imported[0]["value"]
        assert new_id.startswith("upload_")
        client.predict(new_id,api_name="/switch_dataset")
        raw=client.predict("SELECT COUNT(*) AS n FROM orders;","[]",new_id,"raw",api_name="/run_sql")
        assert raw[1]["result"]["rows"]==[[180]]
        print("Extended UI: workflow step navigation, real-ID chart select, pagination/full CSV, SQL full-result check, history and mapped upload: PASS (scripted provider)")
    finally:
        demo.close()


if __name__=="__main__":
    main()
