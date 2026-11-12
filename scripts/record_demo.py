"""Record real browser operations and configured-model calls, then export H.264 MP4."""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".build/browser-check"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--port', type=int, default=7867)
    parser.add_argument('--ffmpeg', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = ROOT / '.build' / ('video-' + uuid4().hex[:10])
    runtime.mkdir(parents=True)

    from playwright.sync_api import sync_playwright, expect
    import app.ui as ui
    from app.providers import ChatCompletionsProvider
    from app.orchestrator import AnalysisAgent
    from app.sql_workspace import SQLWorkspace
    from app.session_store import SessionStore
    from app.models import AnalysisPlan, QueryResult, TimeRange
    from app.oracle import SourceOracle, check_rows
    from app.demo import practice_plans
    from app.semantic import load_manifest
    from app.config import DATASETS
    from check_p1_live import check_shares
    from check_extended_live import check_contribution
    from check_clarification_live import check_details

    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'recording',
              'mode': 'real-browser-real-model', 'dataset_id': 'excel_full',
              'dataset_selector': 'Official DATASETS IDs and display names; uploaded datasets excluded from this demonstration',
              'implementation_sha256': hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in sorted((ROOT/'app').glob('*.py')))).hexdigest(),
              'steps': [], 'events': [], 'model_calls': [], 'page_errors': []}
    def save():
        (output/'demo-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    def capture(action, evidence):
        report['events'].append({'action': action, 'evidence': evidence})
        save()

    class TrackedProvider(ChatCompletionsProvider):
        def complete(self, messages):
            started = time.monotonic()
            response = super().complete(messages)
            report['model_calls'].append({'model': self.model, 'elapsed_seconds': round(time.monotonic()-started, 2),
                                          'response_sha256': hashlib.sha256(response.encode()).hexdigest()})
            save()
            return response

    class TrackedAgent(AnalysisAgent):
        def respond(self, *a, **kw):
            turn = super().respond(*a, **kw)
            capture('respond', turn.evidence)
            return turn
        def drill(self, session, *a, **kw):
            result = super().drill(session, *a, **kw)
            capture('drill', session.last_evidence)
            return result
        def back(self, session):
            result = super().back(session)
            capture('back', session.last_evidence)
            return result

    class TrackedWorkspace(SQLWorkspace):
        def draft(self, *a, **kw):
            draft = super().draft(*a, **kw)
            capture('sql_draft', draft.model_dump())
            return draft
        def execute(self, *a, **kw):
            evidence = super().execute(*a, **kw)
            capture('sql_execute', evidence)
            return evidence

    ui.ChatCompletionsProvider = TrackedProvider
    ui.AnalysisAgent = TrackedAgent
    ui.SQLWorkspace = TrackedWorkspace
    ui.registered_datasets = lambda: dict(DATASETS)
    store = SessionStore(runtime/'sessions.sqlite3')
    ui.SessionStore = lambda: store
    oracle = SourceOracle('excel_full')
    plans = practice_plans(load_manifest('excel_full'))
    demo = ui.build_ui()
    try:
        demo.launch(server_name='127.0.0.1', server_port=args.port, prevent_thread_lock=True, quiet=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width':1440,'height':1080}, device_scale_factor=1,
                                          record_video_dir=str(runtime/'video'), record_video_size={'width':1440,'height':1080}, color_scheme='light', accept_downloads=True)
            page = context.new_page()
            video = page.video
            page.on('pageerror', lambda e: report['page_errors'].append(str(e)))
            started = time.monotonic()
            try:
                page.goto(f'http://127.0.0.1:{args.port}', wait_until='domcontentloaded')
                page.get_by_label('问题', exact=True).wait_for(timeout=30000)
                page.add_style_tag(content='html{scroll-padding-top:110px}body{padding-top:78px!important}#demo-caption{position:fixed;left:0;right:0;top:0;z-index:99999;background:#102b3d;color:#fff;padding:13px 32px;font-family:system-ui,"Microsoft YaHei";box-shadow:0 2px 12px #0002}#demo-caption strong{display:block;font-size:20px}#demo-caption small{color:#bed8e8;font-size:13px}')
                page.evaluate("""() => {const d=document.createElement('div');d.id='demo-caption';d.innerHTML='<strong></strong><small>经营数据分析｜查询、追问与下钻</small>';document.body.append(d)}""")

                def scene(title):
                    report['steps'].append({'title':title, 'seconds':round(time.monotonic()-started,2)})
                    page.locator('#demo-caption strong').evaluate('(el,text)=>el.textContent=text', title)
                    save();print(title, flush=True)
                    page.wait_for_timeout(1600)

                def event_after(count, action):
                    deadline = time.monotonic()+120
                    while len(report['events']) <= count:
                        if time.monotonic()>deadline:raise TimeoutError(action)
                        page.wait_for_timeout(100)
                    event = report['events'][-1]
                    assert event['action']==action, event['action']
                    page.wait_for_timeout(800)
                    return event['evidence']

                def ask(question):
                    field=page.get_by_label('问题',exact=True)
                    field.fill(question)
                    page.wait_for_timeout(1300)
                    count=len(report['events'])
                    page.get_by_role('button',name='分析',exact=True).click()
                    evidence=event_after(count,'respond')
                    expect(field).to_have_value('',timeout=15000)
                    page.wait_for_timeout(1200)
                    return evidence

                def table_view(name):
                    page.get_by_text('本轮查询结果',exact=True).scroll_into_view_if_needed()
                    page.wait_for_timeout(3300)
                    page.screenshot(path=str(output/(name+'.png')))

                def numeric_check(evidence, expected):
                    check_rows(QueryResult.model_validate(evidence['result']),oracle.evaluate(expected))

                scene('01 经营问数｜查询 2025 年 4 月销售额')
                evidence=ask('2025年4月下单且已送达订单的商品销售额是多少？不含运费。')
                april=plans['Q02'].model_copy(update={'time_range':TimeRange(start='2025-04-01',end='2025-05-01')})
                numeric_check(evidence,april)
                assert evidence['result']['rows']==[['193119.68']]
                table_view('01-sales')

                scene('02 连续追问｜与 3 月比较，核对金额和环比')
                evidence=ask('和2025年3月相比呢？给出商品销售额的金额变化和环比，其余口径不变。')
                numeric_check(evidence,plans['Q03'])
                table_view('02-comparison')

                scene('03 图表下钻｜从品类点击进入商家明细')
                evidence=ask('按品类展示2025年4月下单且已送达订单的商品销售额，不含运费，列出全部品类。')
                numeric_check(evidence,april.model_copy(update={'dimension':'category'}))
                page.get_by_text('图表分析',exact=True).click()
                bar=page.locator('[data-group-id="electronics"]').first
                bar.wait_for(state='visible',timeout=15000)
                bar.scroll_into_view_if_needed();page.wait_for_timeout(2500)
                count=len(report['events']);calls=len(report['model_calls'])
                bar.click();evidence=event_after(count,'drill')
                assert evidence['plan']['dimension']=='seller' and evidence['planning_calls']==0
                numeric_check(evidence,AnalysisPlan.model_validate(evidence['plan']))
                assert len(report['model_calls'])==calls
                table_view('03-drill')
                count=len(report['events'])
                page.get_by_role('button',name='返回上层分析',exact=True).click()
                event_after(count,'back');page.wait_for_timeout(1500)

                scene('04 自由 SQL｜生成月销售占比并实际执行')
                evidence=ask('按月列出2025年上半年下单且已送达订单的商品销售额，不含运费，按月份升序。')
                monthly=april.model_copy(update={'dimension':'month','time_range':TimeRange(start='2025-01-01',end='2025-07-01')})
                numeric_check(evidence,monthly)
                table_view('04-monthly')
                page.get_by_text('高级分析 · SQL',exact=True).click()
                page.get_by_label('用自然语言描述 SQL',exact=True).fill('计算每个月商品销售额占这些月份合计的比例。share为0到1，至少保留6位小数或不舍入。输出entity_id、sales、share，按月份升序。')
                page.wait_for_timeout(1400)
                count=len(report['events'])
                page.get_by_role('button',name='生成 SQL 草稿',exact=True).click()
                draft=event_after(count,'sql_draft')
                page.locator('.cm-content').first.scroll_into_view_if_needed()
                page.wait_for_timeout(3500)
                count=len(report['events'])
                page.get_by_role('button',name='执行 SQL',exact=True).click()
                sql_evidence=event_after(count,'sql_execute')
                expected=oracle.evaluate(monthly);total=sum(row['sales'] for row in expected)
                check_shares(QueryResult.model_validate(sql_evidence['result']),[{'entity_id':row['entity_id'],'sales':row['sales'],'share':row['sales']/total} for row in expected])
                table_view('05-sql-result')
                with page.expect_download() as download:
                    page.get_by_text('下载结果 CSV',exact=True).click()
                download.value.save_as(output/'monthly-share.csv')

                scene('05 长期会话｜新建后从本机档案恢复结果')
                session_id=sql_evidence['session_id']
                page.get_by_role('button',name='新建会话',exact=True).click()
                page.wait_for_timeout(1700)
                page.get_by_text('历史会话',exact=True).click()
                page.get_by_label('选择会话',exact=True).click()
                session_title=next(row['title'] for row in store.list('excel_full') if row['session_id']==session_id)
                page.get_by_role('option').filter(has_text=session_title).first.click()
                page.get_by_role('button',name='继续分析',exact=True).click()
                page.get_by_text('本轮查询结果',exact=True).wait_for(timeout=15000)
                table_view('06-restored')
                restored=store.load(session_id,load_manifest('excel_full'))
                assert restored.last_evidence['record_id']==sql_evidence['record_id']

                scene('06 统一澄清｜补年份后继续依赖工作流')
                page.get_by_role('button',name='新建会话',exact=True).click()
                page.wait_for_timeout(1000)
                evidence=ask('先计算4月相对3月各品类对已送达商品销售额净增长的贡献，再列出贡献最高品类4月下单且已送达的订单明细。年份未指定，请先问我年份，并保留两个步骤及依赖关系。')
                assert evidence['type']=='clarify' and evidence['plan']['pending_intent']=='workflow'
                calls=len(report['model_calls'])
                for _ in range(4):
                    if evidence['type']!='clarify':break
                    page.get_by_label('选择补充条件后点击分析',exact=True).click()
                    page.get_by_role('option',name='2025年',exact=True).click()
                    page.wait_for_timeout(1800)
                    evidence=ask('2025年')
                    assert evidence['planning_calls']==0
                assert evidence['type']=='workflow' and len(evidence['steps'])==2
                assert len(report['model_calls'])==calls
                check_contribution(oracle,evidence['steps'][0],plans['Q03'].model_copy(update={'intent':'contribution','dimension':'category'}))
                check_details(oracle,evidence['steps'][1])
                assert evidence['steps'][1]['plan']['filters']==[{'field':'category_id','values':['electronics']}]
                table_view('07-workflow')
                page.get_by_label('分析步骤',exact=True).fill('1')
                page.get_by_role('button',name='查看此步骤结果',exact=True).click()
                page.wait_for_timeout(1200)
                table_view('08-contribution')

                scene('07 分析边界｜缺少促销证据时明确说明')
                evidence=ask('根据现有数据，能否确定4月销售额增长是促销带来的？请说明依据，不要猜测。')
                assert evidence['plan']['intent'] in {'explain','unsupported'} and evidence.get('result') is None
                page.get_by_text('分析对话',exact=True).scroll_into_view_if_needed()
                page.wait_for_timeout(5000)
                page.screenshot(path=str(output/'09-boundary.png'))
                assert not report['page_errors'],report['page_errors']
                report.update(status='passed',real_model_calls=len(report['model_calls']),recording_seconds=round(time.monotonic()-started,2))
                save()
            except Exception as exc:
                report.update(status='failed',error_type=type(exc).__name__)
                page.screenshot(path=str(output/'failure.png'),full_page=True)
                save()
                raise
            finally:
                context.close()
                video.save_as(str(output/'class1-demo.webm'))
                browser.close()
    finally:
        demo.close()

    import subprocess
    target=output/'class1-demo.mp4'
    subprocess.run([str(args.ffmpeg),'-hide_banner','-loglevel','error','-n','-i',str(output/'class1-demo.webm'),
                    '-c:v','libx264','-preset','medium','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',str(target)],check=True)
    subprocess.run([str(args.ffmpeg),'-hide_banner','-loglevel','error','-xerror','-i',str(target),'-f','null','-'],check=True)
    report.update(mp4=str(target),mp4_bytes=target.stat().st_size,mp4_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),full_decode_passed=True)
    save()
    print(json.dumps({k:report[k] for k in ['status','recording_seconds','real_model_calls','mp4','mp4_bytes']},ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
