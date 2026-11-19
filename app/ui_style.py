"""Visual elements of the individual analysis workspace."""
from html import escape


MASTHEAD = """
<header class="desk-mast">
  <div class="desk-title">
    <span class="desk-mark" aria-hidden="true"><i></i><i></i><i></i></span>
    <h1>经营数据分析</h1>
    <p>通过对话分析销售、订单与履约，逐笔核对计算依据。</p>
    <nav aria-label="工作台导航"><a href="#snapshot-row" data-section="snapshot-row">选择快照</a><a href="#data-intake" data-section="data-intake">接入数据</a></nav>
  </div>
  <div class="desk-art" aria-hidden="true">
    <div class="desk-sheet sheet-back"></div><div class="desk-sheet sheet-mid"></div>
    <div class="desk-sheet sheet-front"><i></i><i></i><i></i><i></i><i></i><i></i><i></i></div>
  </div>
</header>
"""


def snapshot_overview(manifest):
    if manifest is None:
        return ""
    counts = manifest.get("normalized_row_counts", {})
    coverage = manifest["coverage"]
    first = coverage.get("first_record")
    last = coverage.get("last_record")
    period = f"<time>{escape(first[:10])}</time> 至 <time>{escape(last[:10])}</time>" if first and last else "未记录"
    orders = format(counts["orders"], ",") if "orders" in counts else "未记录"
    items = format(counts["order_items"], ",") if "order_items" in counts else "未记录"
    return f"""<dl class="desk-metrics" aria-label="所选快照概况">
      <div><dt>快照订单数</dt><dd>{orders}</dd></div>
      <div><dt>商品明细行</dt><dd>{items}</dd></div>
      <div class="desk-period"><dt>下单日期范围</dt><dd>{period}</dd></div>
    </dl>"""


STYLE = """
body:has(#console-masthead), body gradio-app:has(#console-masthead) { background:#edf2f4 !important; }
.gradio-container:has(#console-masthead) {
  --desk-ink:#172b3c; --desk-muted:#506572; --desk-accent:#08745e; --desk-line:#cedadd;
  --desk-card:#fff; --desk-soft:#f3f7f7; --desk-tint:#e3f5ee; --desk-focus:#09765d;
  --body-background-fill:#edf2f4; --background-fill-primary:var(--desk-card);
  --background-fill-secondary:var(--desk-soft); --block-background-fill:var(--desk-card);
  --body-text-color:var(--desk-ink); --body-text-color-subdued:var(--desk-muted);
  --block-label-text-color:var(--desk-muted); --block-title-text-color:var(--desk-ink);
  --border-color-primary:var(--desk-line); --block-border-color:var(--desk-line);
  --input-border-color:var(--desk-line); --color-accent:var(--desk-accent);
  --color-accent-soft:var(--desk-tint); --border-color-accent:var(--desk-accent);
  --font:"Inter","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
  --font-mono:"IBM Plex Mono",Consolas,monospace;
  width:100% !important; max-width:1280px !important; box-sizing:border-box; margin-inline:auto;
  padding:24px clamp(14px,3vw,46px) 48px !important;
  font-family:var(--font); font-variant-numeric:tabular-nums;
}
.dark .gradio-container:has(#console-masthead) {
  --desk-ink:#eaf3f5; --desk-muted:#afc1c6; --desk-accent:#71e6be; --desk-line:#384f5b;
  --desk-card:#172735; --desk-soft:#1d303e; --desk-tint:#203e39; --desk-focus:#77efca;
  --body-background-fill:#0f1c26;
}
.dark gradio-app:has(#console-masthead), body.dark:has(#console-masthead) { background:#0f1c26 !important; }
.gradio-container:has(#console-masthead) > .main { width:100%; padding:0 !important; }
#console-masthead { overflow:hidden; border-radius:18px; }
#console-masthead .html-container, #snapshot-overview .html-container { padding:0; }
.desk-mast {
  position:relative; isolation:isolate; overflow:hidden; min-height:172px;
  display:flex; align-items:center; padding:26px clamp(22px,3.6vw,52px);
  border-radius:18px; background:linear-gradient(108deg,#102136 0%,#152c40 53%,#124c50 100%);
}
.desk-mast::before,.desk-sheet {
  background-image:linear-gradient(90deg,#a0e8d620 1px,transparent 1px),
    linear-gradient(#a0e8d620 1px,transparent 1px); background-size:22px 22px;
}
.desk-mast::before { content:""; position:absolute; inset:0 0 0 40%; z-index:-1; mask-image:linear-gradient(90deg,transparent,#000); }
.desk-title { position:relative; z-index:1; max-width:64%; }
.desk-title h1 { color:#f5fbfc; font-size:clamp(27px,3vw,43px); font-weight:720; letter-spacing:.045em; line-height:1.22; margin:9px 0 6px; }
.desk-title p { color:#c2d4de; font-size:14px; line-height:1.6; margin:0; }
.desk-title nav { display:flex; flex-wrap:wrap; gap:20px; margin-top:12px; }
.desk-title nav a { display:inline-flex; gap:6px; align-items:center; padding:4px 0; color:#a1f4d7; font-size:13px; font-weight:600; text-decoration:none; }
.desk-title nav a::after { content:"↗"; font-size:12px; } .desk-title nav a:hover { text-decoration:underline; }
.desk-mark { display:flex; gap:3px; height:18px; align-items:end; }
.desk-mark i { display:block; width:5px; height:8px; transform:skewX(-13deg); background:#55e3b6; }
.desk-mark i:nth-child(2) { height:13px; } .desk-mark i:last-child { height:18px; }
.desk-art { position:absolute; right:5%; top:9%; height:82%; width:30%; pointer-events:none; }
.desk-sheet {
  position:absolute; right:2%; top:6%; width:79%; height:66%; border:1px solid #77c9bc;
  border-radius:8px; transform:matrix(.9,-.28,.52,.78,0,0); box-shadow:6px 10px 22px #051b292f;
}
.sheet-back { margin:31px 18px 0 0; background-color:#1a5757; border-color:#488e87; }
.sheet-mid { margin:16px 9px 0 0; background-color:#23746a; border-color:#53b6a0; }
.sheet-front { display:flex; align-items:flex-end; justify-content:center; gap:6%; padding:8% 12% 5%; background-color:#174e51; }
.sheet-front i { height:36%; flex:1; background:#76e8ba; border-radius:2px 2px 0 0; }
.sheet-front i:nth-child(2) { height:62%; } .sheet-front i:nth-child(3) { height:48%; }
.sheet-front i:nth-child(4) { height:81%; } .sheet-front i:nth-child(5) { height:64%; }
.sheet-front i:nth-child(6) { height:99%; } .sheet-front i:last-child { height:78%; }
#snapshot-row, #conversation-row, #result-panel, #data-intake, #empty-notice {
  border:1px solid var(--desk-line); border-radius:14px; background:var(--desk-card);
}
#snapshot-row { display:grid; grid-template-columns:minmax(190px, 1fr) minmax(0, 2fr); align-items:center; gap:24px; padding:14px 22px; }
#snapshot-row:not(:has(.desk-metrics)) { grid-template-columns:1fr; }
#snapshot-row, #data-intake { scroll-margin-top:12px; }
#snapshot-row .form, #composer .form { border:0; box-shadow:none; background:transparent; }
.desk-metrics { display:grid; grid-template-columns:1fr 1fr 1.8fr; align-items:center; gap:0; margin:0; }
.desk-metrics div { min-width:0; border-left:1px solid var(--desk-line); padding:0 clamp(10px,1.8vw,25px); }
.desk-metrics dt { font-size:12px; font-weight:600; color:var(--desk-muted); margin:0 0 4px; }
.desk-metrics dd { margin:0; color:var(--desk-ink); font:650 22px/1.25 var(--font); font-variant-numeric:tabular-nums; }
.desk-period dd { font-size:14px; overflow-wrap:anywhere; }
.desk-period time { white-space:nowrap; }
#conversation-row { padding:14px; align-items:stretch; gap:18px; }
#conversation-log { border:1px solid var(--desk-line); border-radius:10px; background:var(--desk-soft); }
#composer { padding:6px 6px 0; gap:8px; }
.gradio-container h2.desk-section { display:flex; align-items:center; gap:9px; margin:0; font-size:19px; font-weight:700; color:var(--desk-ink); }
.desk-section::before { content:""; width:4px; height:17px; background:var(--desk-accent); border-radius:4px; }
.desk-chat-empty { display:grid; justify-items:center; gap:14px; color:var(--desk-muted); font-size:14px; }
.desk-chat-empty::before { content:""; width:38px; height:31px; border:2px solid var(--desk-accent);
  border-radius:12px 12px 12px 3px; background:linear-gradient(var(--desk-accent),var(--desk-accent)) 9px 9px/18px 2px no-repeat,
    linear-gradient(var(--desk-accent),var(--desk-accent)) 9px 17px/10px 2px no-repeat,var(--desk-tint); }
#result-panel { padding:14px; }
.gradio-container:has(#console-masthead) .gr-accordion { background:var(--desk-card); border-color:var(--desk-line); border-radius:10px; }
#composer textarea { min-height:56px; }
#composer-actions { gap:8px; }
#history-panel { margin-top:4px; }
#data-intake { padding:6px; }
#empty-notice { padding:14px 20px; border-left:4px solid var(--desk-accent); color:var(--desk-ink); }
.gradio-container:has(#console-masthead) :is(button, a, input, textarea, select):focus-visible {
  outline:3px solid var(--desk-focus); outline-offset:2px;
}
#run-analysis, #identify-files, #confirm-import { background:#086954; border:1px solid #086954; color:white; font-weight:650; min-height:42px; }
#run-analysis:hover, #identify-files:hover, #confirm-import:hover { background:#075642; }
#new-analysis { min-height:42px; }
#query-result { border-color:var(--desk-line); border-radius:12px; }
.desk-charts { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(290px,100%),1fr)); gap:12px; }
.desk-charts section { min-width:0; border:1px solid var(--desk-line); border-radius:10px; padding:12px; background:var(--desk-card); }
.desk-charts h3 { margin:0 0 8px; color:var(--desk-ink); }
.desk-charts .chart-row { width:100%; display:grid; grid-template-columns:minmax(0,1.1fr) minmax(14%,1fr) minmax(0,1fr);
  align-items:center; gap:8px; padding:7px 4px; border:0; color:var(--desk-ink); background:transparent; text-align:left; overflow-wrap:anywhere; }
.desk-charts button.chart-row:hover { background:var(--desk-soft); cursor:pointer; }
.chart-row > :last-child { text-align:right; font-variant-numeric:tabular-nums; }
.chart-track { height:10px; background:var(--desk-line); border-radius:3px; overflow:hidden; }
.chart-track > span { display:block; height:100%; background:var(--desk-accent); }
.chart-track .negative { background:#c96c37; } .chart-unknown { padding:7px 4px; color:var(--desk-muted); }
#upload-input { background:var(--desk-soft); border-radius:12px; }
@media(max-width:850px) {
  #snapshot-row { gap:10px; } .desk-art { width:34%; }
  .desk-metrics div { padding:0 10px; }
}
@media(max-width:650px) {
  .gradio-container:has(#console-masthead) { padding:12px 12px 32px !important; }
  .desk-mast { min-height:125px; padding:16px 18px; }
  .desk-art { right:-4%; top:15%; width:37%; opacity:.75; }
  .desk-title { max-width:73%; } .desk-title p { font-size:12px; }
  #snapshot-row, #conversation-row { display:flex; flex-direction:column; align-items:stretch; gap:10px; padding:12px; }
  #snapshot-row > *, #conversation-row > * { min-width:0 !important; width:100%; }
  .desk-metrics { grid-template-columns:1fr 1fr 1.65fr; }
  .desk-metrics div:first-child { border:0; padding-left:0; } .desk-metrics dd { font-size:17px; }
  .desk-metrics .desk-period dd { font-size:12px; } #composer { padding:0; }
  #conversation-log { flex:none !important; height:206px !important; min-height:0 !important; }
  #conversation-log:has(.desk-chat-empty) { height:94px !important; }
  .desk-chat-empty { grid-auto-flow:column; align-items:center; gap:9px; }
  #composer { flex:none !important; } .desk-title h1 { letter-spacing:.015em; }
}
@media(prefers-reduced-motion:reduce) {
  #run-analysis, #identify-files, #confirm-import { transition:none; }
}
"""
