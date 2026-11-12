"""新意图与工作流的界面澄清回放；固定Provider，不调用模型。"""
import hashlib
import json
from copy import deepcopy
from tempfile import TemporaryDirectory
from pathlib import Path

from gradio_client import Client

from app.config import ROOT, RUNS_DIR
from app.demo import practice_plans
from app.executor import Executor
from app.models import EntityFilter, TimeRange
from app.semantic import load_manifest
from app.session_store import SessionStore


class ScriptedProvider:
    model = "clarification-ui-scripted"
    calls = 0

    def complete(self, messages):
        self.calls += 1
        context = json.loads(messages[1]["content"])
        manifest = context["catalog"]
        contribution = practice_plans(load_manifest(manifest["dataset_id"]))["Q03"].model_copy(update={
            "intent": "clarify", "pending_intent": "contribution", "dimension": "category",
            "time_range": None, "comparison_range": None, "month": 4, "comparison_month": 3,
            "missing_slots": ["year"], "clarification": "请补充年份",
            "filters": [EntityFilter(field="seller_id", values=[manifest["entities"]["sellers"][0][0]])]})
        details = contribution.model_copy(update={"intent": "details", "pending_intent": None,
            "metric_ids": [], "filters": [], "missing_slots": [], "clarification": None,
            "month": None, "comparison_month": None, "time_range": TimeRange(start="2025-04-01", end="2025-05-01"),
            "source_step": 1, "bind_field": "category_id"})
        return contribution.model_copy(update={"intent": "workflow", "pending_intent": None,
            "missing_slots": [], "clarification": None, "filters": [], "steps": [contribution, details]}).model_dump_json()


def main():
    import app.ui as ui
    original_executor, original_provider, original_store = ui.Executor, ui.ChatCompletionsProvider, ui.SessionStore

    def with_duplicate_names(dataset):
        executor = Executor(dataset)
        executor.manifest = deepcopy(executor.manifest)
        for row in executor.manifest["entities"]["sellers"][:2]:
            row[1] = "同名商家"
        return executor

    with TemporaryDirectory(prefix="clarification-ui-") as directory:
        provider = ScriptedProvider()
        ui.Executor = with_duplicate_names
        ui.ChatCompletionsProvider = lambda: provider
        ui.SessionStore = lambda: SessionStore(Path(directory) / "sessions.sqlite3")
        demo = ui.build_ui()
        try:
            demo.launch(server_name="127.0.0.1", server_port=7866, prevent_thread_lock=True, quiet=True)
            client = Client("http://127.0.0.1:7866/", verbose=False)
            pending = client.predict("同名商家4月比3月的品类增长贡献，再查最高品类订单", [], "csv_small", False, api_name="/submit")
            assert pending[2]["type"] == "clarify" and "步骤1" in pending[2]["plan"]["clarification"]
            assert pending[8]["visible"] and ["2025年", "2025年"] in pending[8]["choices"]
            year = client.predict("2025年", pending[1], "csv_small", False, api_name="/submit")
            assert year[2]["planning_calls"] == 0 and len(year[8]["choices"]) == 2
            assert year[8]["visible"] and all("同名商家" in label for label, _ in year[8]["choices"])
            selected = year[8]["choices"][1][1]
            completed = client.predict(selected, year[1], "csv_small", False, api_name="/submit")
            evidence = completed[2]
            assert evidence["type"] == "workflow" and evidence["planning_calls"] == 0 and provider.calls == 1
            assert not completed[8]["visible"]
            contribution, details = evidence["steps"]
            assert contribution["result"]["status"] == details["result"]["status"] == "ok"
            assert contribution["plan"]["filters"][0]["values"] == [selected]
            category = contribution["result"]["rows"][0][0]
            assert details["plan"]["filters"] == [{"field": "category_id", "values": [category]}]
            report = {"mode": "scripted-provider-ui", "passed": True, "provider_calls": provider.calls,
                "implementation_sha256": hashlib.sha256(b"".join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/"app").glob("*.py")))).hexdigest(),
                "checks": ["workflow-year-choices", "entity-id-choices", "no-model-slot-completion", "dependent-query-execution"],
                "evidence_id": evidence["record_id"]}
            (RUNS_DIR/"clarification-ui.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Workflow year/entity choices + zero-model completion + real dependency execution: PASS")
        finally:
            demo.close()
            ui.Executor, ui.ChatCompletionsProvider, ui.SessionStore = original_executor, original_provider, original_store


if __name__ == "__main__":
    main()
