"""本机 Gradio 提交链路检查；固定计划 Provider，不调用真实模型。"""


class ScriptedProvider:
    model = "ui-smoke-scripted-provider"

    def __init__(self):
        self.calls = 0

    def complete(self, messages):
        import json

        from app.demo import practice_plans
        from app.semantic import load_manifest

        context = json.loads(messages[1]["content"])
        plans = practice_plans(load_manifest(context["catalog"]["dataset_id"]))
        self.calls += 1
        plan = plans["Q08"] if self.calls == 3 else plans["Q02"]
        if self.calls == 2:
            plan = plan.model_copy(update={"dimension": "month"})
        elif self.calls == 4:
            plan = plans["Q04"]
        elif self.calls == 5:
            assert context["last_successful_plan"] is None and context["pending_plan"] is None
        elif self.calls == 6:
            plan = plan.model_copy(update={"intent": "clarify", "time_range": None, "clarification": "请补充年份",
                "missing_slots": ["year"], "pending_intent": "aggregate", "month": 4})
        elif self.calls == 7:
            plan = plan.model_copy(update={"intent": "join_example", "metric_ids": []})
        return plan.model_dump_json()


def main():
    import csv
    import json
    from pathlib import Path

    import httpx
    from gradio_client import Client

    import app.ui as ui

    ui.ChatCompletionsProvider = ScriptedProvider
    demo = ui.build_ui()
    try:
        demo.launch(server_name="127.0.0.1", server_port=7862, prevent_thread_lock=True, quiet=True)
        assert httpx.get("http://127.0.0.1:7862/").status_code == 200
        client = Client("http://127.0.0.1:7862/", verbose=False)
        result = client.predict("全期商品销售额", [], "csv_small", True, api_name="/submit")
        assert result[2]["result"]["rows"] == [["75484.21"]]
        assert result[2]["model"] == "ui-smoke-scripted-provider"
        assert result[2]["planning_calls"] == 1
        assert len(result[1]) == 2
        assert result[3]["visible"] and not result[4]["visible"] and not result[5]["visible"]
        with open(result[6]["value"], encoding="utf-8-sig", newline="") as handle:
            assert list(csv.DictReader(handle)) == [{"sales": "75484.21"}]
        downloaded = json.loads(Path(result[7]["value"]).read_text(encoding="utf-8"))
        assert downloaded["result_sha256"] == result[2]["result_sha256"]
        monthly = client.predict("按月看商品销售额", result[1], "csv_small", True, api_name="/submit")
        assert monthly[5]["visible"] and not monthly[4]["visible"]
        assert len(monthly[2]["result"]["rows"]) == 6
        explanation = client.predict("解释为什么重复", monthly[1], "csv_small", True, api_name="/submit")
        assert explanation[2]["type"] == "explain"
        assert not explanation[3]["visible"] and not explanation[5]["visible"] and not explanation[6]["visible"]
        ranked = client.predict("4月销售额最高品类", explanation[1], "csv_small", True, api_name="/submit")
        assert ranked[4]["visible"] and not ranked[5]["visible"]
        assert ranked[2]["result"]["rows"][0][2] == "8564.60"
        switched = client.predict("excel_full", api_name="/switch_dataset")
        assert switched[0] == [] and switched[1] == {} and not switched[6]["visible"]
        excel = client.predict("全期商品销售额", [], "excel_full", True, api_name="/submit")
        assert excel[2]["result"]["rows"] == [["4858920.84"]]
        pending = client.predict("4月商品销售额", excel[1], "excel_full", False, api_name="/submit")
        assert pending[8]["visible"] and ["2025年", "2025年"] in pending[8]["choices"]
        resolved = client.predict("2025年", pending[1], "excel_full", False, api_name="/submit")
        assert resolved[2]["planning_calls"] == 0 and resolved[2]["result"]["rows"] == [["193119.68"]]
        example = client.predict("用全期真实订单举例", resolved[1], "excel_full", True, api_name="/submit")
        assert example[2]["plan"]["intent"] == "join_example" and example[2]["result"]["rows"][0][3] > 1
        print("UI charts/downloads + dataset switch + slot choices without model recall + real join example: PASS (scripted provider)")
    finally:
        demo.close()


if __name__ == "__main__":
    main()
