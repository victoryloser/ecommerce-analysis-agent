"""Headless browser acceptance with synthetic data and a fixed planner."""
import argparse
import csv
import gc
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / '.build/browser-check')]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=7873)
    parser.add_argument('--browser-channel',default='msedge')
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright
    import app.ui as ui
    import app.config, app.evidence, app.presentation, app.trace
    from app.executor import Executor
    from app.import_data import import_tables
    from app.calculation import ROUND2_RULE
    from app.session_store import SessionStore
    from app.source import SCHEMA
    from test_round2_amounts import source_tables, plan_for

    with TemporaryDirectory(prefix='r1-ui-',dir=ROOT / '.build') as temporary:
        work = Path(temporary)
        tables = source_tables(False)
        tables['order_items'].append(dict(tables['order_items'][0]))
        files = []
        for table, rows in tables.items():
            path = work / (table + '.csv')
            with path.open('w',encoding='utf-8-sig',newline='') as handle:
                writer = csv.DictWriter(handle, fieldnames=list(SCHEMA[table]))
                writer.writeheader()
                writer.writerows(rows)
            files.append(path)
        manifest = import_tables('r1-ui',tables,files,'R1 browser data',work,quality_rule_version=ROUND2_RULE)
        store = SessionStore(work / 'sessions.sqlite3')
        for module in (app.config, app.evidence, app.presentation, app.trace):
            module.RUNS_DIR = work / 'runs'

        class Scripted:
            model = 'r1-browser-fixed'
            calls = 0
            def complete(self,messages):
                self.calls += 1
                if self.calls == 1:
                    return plan_for(manifest,intent='compare',comparison_range={'start':'2027-03-01','end':'2027-04-01'}).model_dump_json()
                return plan_for(manifest,output_mode='seller_audit',top_n=3).model_dump_json()

        ui.registered_datasets = lambda: {'r1-ui':manifest['name']}
        ui.Executor = lambda dataset: Executor(dataset,work)
        ui.SessionStore = lambda: store
        ui.ChatCompletionsProvider = Scripted
        demo = ui.build_ui()
        try:
            demo.launch(server_name='127.0.0.1',server_port=args.port,prevent_thread_lock=True,quiet=True,footer_links=[])
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=args.browser_channel,headless=True)
                page = browser.new_page()
                page.goto(demo.local_url,wait_until='domcontentloaded')
                question = page.get_by_label('问题',exact=True)
                question.fill('比较两个月的订单数、商品额和运费')
                page.get_by_role('button',name='分析',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('商品额差额')",timeout=30000)
                session_id = store.list('r1-ui')[0]['session_id']
                first = store.load(session_id,manifest)
                page.get_by_role('button',name='查看质量依据',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('完全重复商品明细') && document.body.innerText.includes('O2')",timeout=30000)
                checked = store.load(session_id,manifest)
                assert checked.working_plan == first.working_plan and checked.analysis_anchor == first.analysis_anchor
                page.get_by_text('M',exact=True).last.click()
                page.get_by_text('问题原始记录',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('logical_csv_record')",timeout=30000)
                page.get_by_role('button',name='导出全部质量记录',exact=True).click()
                page.get_by_role('button',name='下载全部质量记录',exact=True).wait_for(state='visible',timeout=30000)
                question.fill('商家前三名并任选第一名订单核查')
                page.get_by_role('button',name='分析',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('选中商家商品小计')",timeout=30000)
                page.get_by_role('button',name='导出全部质量记录',exact=True).wait_for(state='hidden',timeout=30000)
                session = store.load(session_id,manifest)
                assert all(t['status']=='completed' for t in session.workflow_tasks)
                page.get_by_label('分析步骤',exact=True).fill('3')
                page.get_by_role('button',name='查看此步骤结果',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('支付记录数')",timeout=30000)
                page.get_by_label('分析步骤',exact=True).fill('4')
                page.get_by_role('button',name='查看此步骤结果',exact=True).click()
                page.wait_for_function("document.body.innerText.includes('属于选中商家')",timeout=30000)
                current = store.load(session_id,manifest)
                assert len(current.last_result.rows) == 2 and len(current.workflow_tasks) == 4
                page.screenshot(path=str(ROOT / '.build/r1-ui.png'),full_page=True)
                browser.close()
            print('PASS: comparison, two-period quality, source record click, full export, seller audit and all-item view; fixed planner')
        finally:
            demo.close()
            gc.collect()


if __name__ == '__main__':
    main()
