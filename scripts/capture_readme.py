"""使用虚构记录和固定模型响应生成 README 截图，不读取本地业务数据。"""
import argparse
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=7871)
    parser.add_argument("--browser-channel", help="例如 msedge；默认使用 Playwright Chromium")
    args = parser.parse_args()
    # 兼容已有本地浏览器工具；新环境可直接安装 Playwright。
    browser_tools = ROOT / ".build/browser-check"
    if browser_tools.is_dir():
        sys.path.insert(0, str(browser_tools))
    from playwright.sync_api import sync_playwright
    import app.ui as ui
    import app.evidence as evidence
    import app.presentation as presentation
    import app.trace as trace
    from app.data_upload import registered_datasets, import_upload
    from app.executor import Executor
    from app.models import AnalysisPlan, TimeRange
    from app.session_store import SessionStore

    class ScreenshotProvider:
        model = "documentation-scripted-provider"

        def complete(self, messages):
            context = json.loads(messages[1]["content"])
            return AnalysisPlan(dataset_id=context["catalog"]["dataset_id"], metadata_version="1.0.0",
                                intent="aggregate", metric_ids=["sales", "order_count", "payment_amount"],
                                time_range=TimeRange(start="2025-01-01", end="2025-02-01")).model_dump_json()

    output = ROOT / "docs/images"
    output.mkdir(parents=True, exist_ok=True)
    build = ROOT / ".build"
    build.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="readme-capture-", dir=build) as directory:
        work = Path(directory)
        assert work.resolve().is_relative_to(build.resolve())
        data_dir = work / "data"
        runs_dir = work / "runs"
        for module in (evidence, presentation, trace):
            module.RUNS_DIR = runs_dir
        files = runpy.run_path(str(ROOT / "tests/test_data_onboarding.py"))["write_business_files"]("xlsx", work)
        ui.registered_datasets = lambda: registered_datasets(data_dir)
        ui.Executor = lambda dataset: Executor(dataset, data_dir)
        ui.SessionStore = lambda: SessionStore(data_dir / "sessions.sqlite3")
        ui.import_upload = lambda files, mapping, name: import_upload(files, mapping, name, data_dir)
        ui.ChatCompletionsProvider = ScreenshotProvider
        demo = ui.build_ui()
        try:
            demo.launch(server_name="127.0.0.1", server_port=args.port, prevent_thread_lock=True,
                        quiet=True, footer_links=[])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=args.browser_channel, headless=True)
                page = browser.new_page(viewport={"width": 1360, "height": 920}, device_scale_factor=1)
                page.goto(demo.local_url, wait_until="domcontentloaded")
                page.locator('input[type="file"]').wait_for(state="attached")
                page.get_by_text("数据文件", exact=True).wait_for(state="visible")
                page.evaluate("document.fonts.ready")
                page.screenshot(path=str(output / "first-run.png"), full_page=True)
                page.locator('input[type="file"]').set_input_files(files)
                page.get_by_role("button", name="识别文件", exact=True).click()
                page.get_by_text("核对字段对应关系（可修改来源文件、工作表和来源列名）", exact=True).wait_for(state="visible", timeout=30000)
                page.get_by_label("数据集名称", exact=True).fill("演示数据（虚构）")
                page.get_by_role("button", name="校验映射并导入", exact=True).click()
                question = page.get_by_label("问题", exact=True)
                question.wait_for(state="visible", timeout=30000)
                page.get_by_text("接入新的Excel/CSV数据", exact=True).click()
                question.fill("2025年1月已送达订单的商品销售额、订单量和支付金额是多少？")
                page.get_by_role("button", name="分析", exact=True).click()
                page.wait_for_function("document.body.innerText.includes('14.50')", timeout=30000)
                page.get_by_text("本轮查询结果", exact=True).wait_for(state="visible")
                page.get_by_role("button", name="导出全部结果", exact=True).wait_for(state="visible", timeout=30000)
                page.evaluate("document.fonts.ready")
                page.screenshot(path=str(output / "analysis.png"), full_page=True)
                browser.close()
            print("README screenshots: docs/images/first-run.png, docs/images/analysis.png")
        finally:
            demo.close()


if __name__ == "__main__":
    main()
