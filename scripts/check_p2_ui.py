"""固定模型、临时构造数据：真实浏览器核对联合分组下钻和月度多序列。"""
import argparse
import gc
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7872)
    parser.add_argument("--browser-channel", default="msedge")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / ".build/browser-check"))
    sys.path.insert(0, str(ROOT / "tests"))
    from playwright.sync_api import sync_playwright
    import app.ui as ui
    import app.evidence as evidence
    import app.presentation as presentation
    import app.trace as trace
    from app.executor import Executor
    from app.session_store import SessionStore
    from test_joint_analysis import joint_tables, tables, build, plan_for

    class Scripted:
        model = "p2-browser-scripted"
        calls = 0

        def complete(self, messages):
            self.calls += 1
            return plan_for(manifest, metric_ids=["sales"],
                group_by=["category", "seller"] if self.calls == 1 else ["month", "category"],
                time_range={"start": "2027-03-01", "end": "2027-05-01"}).model_dump_json()

    work_root = ROOT / ".build"
    work_root.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2-ui-", dir=work_root) as directory:
        work = Path(directory)
        manifest = build(work, joint_tables.__wrapped__(tables.__wrapped__()))
        store = SessionStore(work / "sessions.sqlite3")
        for module in (evidence, presentation, trace):
            module.RUNS_DIR = work / "runs"
        ui.registered_datasets = lambda: {manifest["dataset_id"]: manifest["name"]}
        ui.Executor = lambda dataset: Executor(dataset, work)
        ui.SessionStore = lambda: store
        ui.ChatCompletionsProvider = Scripted
        demo = ui.build_ui()
        try:
            demo.launch(server_name="127.0.0.1", server_port=args.port, prevent_thread_lock=True,
                        quiet=True, footer_links=[])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=args.browser_channel, headless=True)
                page = browser.new_page()
                page.goto(demo.local_url, wait_until="domcontentloaded")
                question = page.get_by_label("问题", exact=True)
                question.fill("2027年3至4月按品类和商家查看商品销售额")
                page.get_by_role("button", name="分析", exact=True).click()
                page.get_by_text("图表分析", exact=True).click()
                bar = page.locator("[data-group-id]").first
                bar.wait_for(state="visible", timeout=30000)
                selected = json.loads(bar.get_attribute("data-group-id"))
                bar.click()
                page.wait_for_function("document.body.innerText.includes('订单ID')", timeout=30000)
                session_id = store.list(manifest["dataset_id"])[0]["session_id"]
                session = store.load(session_id, manifest)
                drilled = session.last_evidence
                assert drilled["plan"]["intent"] == "details"
                assert {f["field"]: f["values"] for f in drilled["plan"]["filters"]} == {
                    "category_id": [selected["category"]], "seller_id": [selected["seller"]]}
                assert drilled["planning_calls"] == 0
                page.get_by_role("button", name="返回上层分析", exact=True).click()
                page.locator("[data-group-id]").first.wait_for(state="visible", timeout=30000)
                question.fill("2027年3至4月按月份和品类查看商品销售额趋势")
                page.get_by_role("button", name="分析", exact=True).click()
                page.wait_for_function("document.body.innerText.includes('K1 [K1]') && document.body.innerText.includes('K2 [K2]')", timeout=30000)
                session = store.load(session_id, manifest)
                assert session.last_evidence["plan"]["group_by"] == ["month", "category"]
                assert len({row["品类"] for row in presentation.present(session.last_evidence).chart}) == 2
                assert page.locator("svg").count() > 0
                browser.close()
            print("PASS: compound-key DOM click, both filters, no extra model call, back navigation, monthly category series")
        finally:
            demo.close()
            gc.collect()


if __name__ == "__main__":
    main()
