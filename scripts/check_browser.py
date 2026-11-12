"""本机Edge真实点击核对，Playwright只安装在.build临时目录，不作为运行依赖。"""
import hashlib
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/".build/browser-check"))


class Scripted:
    model="browser-scripted"
    def complete(self,messages):
        from app.demo import practice_plans
        from app.semantic import load_manifest
        context=json.loads(messages[1]["content"])
        return practice_plans(load_manifest(context["catalog"]["dataset_id"]))["Q04"].model_dump_json()


def main():
    import app.ui as ui
    from playwright.sync_api import sync_playwright
    ui.ChatCompletionsProvider=Scripted
    demo=ui.build_ui()
    try:
        demo.launch(server_name="127.0.0.1",server_port=7865,prevent_thread_lock=True,quiet=True,footer_links=[])
        with sync_playwright() as p:
            browser=p.chromium.launch(channel="msedge",headless=True)
            page=browser.new_page()
            page.goto("http://127.0.0.1:7865",wait_until="domcontentloaded")
            page.get_by_label("问题",exact=True).wait_for(state="visible",timeout=30000)
            page.screenshot(path=str(ROOT/".build/user-ui-overview.png"), full_page=True)
            page.get_by_label("问题",exact=True).fill("按品类查看2025年4月商品销售额")
            page.get_by_role("button",name="分析",exact=True).click()
            assert page.get_by_text("示例上下文", exact=False).count() == 0
            assert page.get_by_text("分析计划、SQL与结果证据", exact=False).count() == 0
            page.get_by_text("图表分析",exact=True).click()
            bar=page.locator("[data-group-id]").first
            bar.wait_for(state="visible",timeout=30000)
            selected=bar.get_attribute("data-group-id")
            bar.click()
            page.wait_for_function("document.body.innerText.includes('已按商家汇总')",timeout=30000)
            page.screenshot(path=str(ROOT/".build/user-ui-results.png"), full_page=True)
            browser.close()
        report={"mode":"scripted-browser","passed":True,"browser":"Microsoft Edge headless","selected_entity_id":selected,
            "check":"real DOM click on chart bar invokes host drilldown and renders seller result",
            "implementation_sha256":hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/'app').glob('*.py')))).hexdigest()}
        (ROOT/"runs/browser-check.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print("Actual Edge DOM click -> stable-ID chart drill -> seller result: PASS (scripted provider)")
    finally:
        demo.close()


if __name__=="__main__":
    main()
