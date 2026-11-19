from app.weekly_page import PANEL, SCRIPT

PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>团队经营分析</title>
<style>
:root{color-scheme:light;--ink:#101e31;--muted:#4e6170;--line:#d6e0e4;--teal:#096b60;--mint:#40e0b4;--pale:#eef6f4}
*{box-sizing:border-box}
body{margin:0;background:#f1f5f6;color:var(--ink);font:16px/1.55 "Inter","Microsoft YaHei UI","PingFang SC",system-ui,sans-serif}
main{max-width:1280px;margin:auto;padding:24px clamp(12px,3.5vw,48px) 56px}
header{isolation:isolate;position:relative;overflow:hidden;border-radius:18px;background:var(--ink);color:white;
 padding:clamp(20px,3vw,40px);box-shadow:0 9px 24px #101e3114}
header::before{content:"";position:absolute;inset:0 0 0 45%;z-index:-2;
 background:linear-gradient(90deg,var(--ink),transparent 25%),repeating-linear-gradient(0deg,transparent 0 27px,#5ba3952b 27px 28px),
 repeating-linear-gradient(90deg,transparent 0 27px,#5ba3952b 27px 28px),linear-gradient(100deg,#102334,#1a3942)}
header::after{content:"";position:absolute;top:13%;right:-2%;bottom:8%;width:48%;z-index:-1;opacity:.78;
 background:linear-gradient(100deg,#239d92,#40e0b4);clip-path:polygon(0 95%,20% 59%,31% 72%,51% 32%,64% 48%,91% 5%,100% 12%,
 100% 14%,91% 7%,64% 50%,51% 34%,31% 74%,20% 61%,0 97%)}
.heading-line,.row,.actions,nav{display:flex;align-items:center;flex-wrap:wrap;gap:10px}
h1,h2,p{margin:0}
h1{font-size:clamp(28px,3.4vw,46px);line-height:1.25;font-weight:750;letter-spacing:-.045em}
.brand-mark{display:inline-grid;place-items:center;flex:none;width:42px;height:42px;border:1px solid #75ecd763;border-radius:11px;background:#174740}
.brand-mark::before{content:"";width:22px;height:22px;background:var(--mint);
 clip-path:polygon(0 65%,24% 65%,24% 100%,0 100%,0 65%,36% 65%,36% 32%,61% 32%,61% 100%,36% 100%,36% 65%,76% 65%,76% 0,100% 0,100% 100%,76% 100%,76% 65%)}
#identity{margin-inline-start:6px;border-left:1px solid #709890;padding-inline-start:14px;color:#c4eee4;font-weight:600;overflow-wrap:anywhere}
#identity:empty{display:none}
nav{gap:16px;margin-top:14px}
nav a{color:#d8eee9;text-decoration:none;font-size:14px;padding:8px 0;display:inline-flex;align-items:center;min-height:44px}
nav a:hover{color:white;text-decoration:underline;text-underline-offset:5px}
.workspace{display:grid;grid-template-columns:minmax(0,2fr) minmax(260px,1fr);gap:18px;margin:18px 0;align-items:start}
.main-column,aside{display:grid;align-content:start;gap:14px;min-width:0}
section,aside>details{min-width:0;padding:clamp(16px,2vw,26px);border:1px solid var(--line);background:white;border-radius:13px}
#analysis{border-top:3px solid var(--teal)}
h2{font-size:21px;line-height:1.35;letter-spacing:-.025em;margin-bottom:16px}
.row,.actions{row-gap:8px}.actions{margin:10px 0 16px}
.analysis-tools{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.analysis-tools>details{margin:0}.analysis-tools>details[open]{grid-column:1/-1}
.snapshot-row{margin-bottom:16px;padding:10px 12px;background:#f4f8f9;border:1px solid #e0e8ea;border-radius:8px}
button,input,select,textarea{font:inherit;color:inherit;max-width:100%;min-width:0;vertical-align:middle}
button,input:not([type=checkbox]),select,textarea{border:1px solid #abbfc4;border-radius:7px;min-height:44px;padding:8px 11px;background:white}
button{font-size:14px;font-weight:650;cursor:pointer;color:#20464b;transition:background-color .15s ease,border-color .15s ease}
button:hover{background:var(--pale);border-color:#62998e}
button.primary{background:var(--teal);border-color:var(--teal);color:white;padding-inline:22px}
button.primary:hover{background:#09564d}
button:disabled{opacity:.6;cursor:wait}
#report:disabled,#viewWeekly:disabled{cursor:default;color:var(--muted);background:#f3f6f7}
input[type=checkbox]{accent-color:var(--teal)}
label{font-weight:550;font-size:14px;color:#384e5c}
label>select,label>input{margin:2px}
textarea{width:100%;resize:vertical}
#question{background:#fafcfc;margin-top:8px}
.question-label{display:block;margin:12px 0 4px}
#examples,#reports{width:100%}
#report{margin-top:8px}
details{min-width:0;margin-top:8px;padding:0 12px;border:1px solid var(--line);border-radius:8px}
aside>details{margin:0;padding:2px 16px}
summary{min-height:44px;cursor:pointer;align-content:center;font-weight:650;font-size:15px;color:#253b4a}
summary::marker{color:var(--teal)}
details[open]{padding-bottom:14px}details[open]>summary{margin-bottom:10px;border-bottom:1px solid var(--line)}
#weeklyPanel .row+div,#weeklyAnalyst>.row{margin-top:12px}
details p,#weeklyAnalyst>p{margin:8px 0;color:var(--muted);font-size:14px}
details button,details input,details select{margin-block:3px}
pre{margin:12px 0;white-space:pre-wrap;overflow-wrap:anywhere;font:14px/1.7 "IBM Plex Mono",Consolas,monospace;color:#314956}
table{border-collapse:collapse;width:100%;font-size:14px;font-variant-numeric:tabular-nums}
th,td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:left}
th{color:#294856;background:#edf4f5;white-space:nowrap}td{overflow-wrap:anywhere}
#result,#opsDaily{overflow-x:auto;max-width:100%}#status,#opsSummary,#jobList{overflow-wrap:anywhere}
#result:not(:empty){margin-top:10px}#result>.answer{font-family:inherit;font-size:16px;color:var(--ink)}
#status:not(:empty){display:inline-block;color:var(--teal);font-size:14px;font-weight:650}
.empty-result{display:none}#output:has(#result:empty):has(#status:empty) .empty-result{display:block}
.empty-result,.empty-list{color:var(--muted);font-size:14px}
a{color:var(--teal)}:focus-visible{outline:3px solid #208a84;outline-offset:3px}header :focus-visible{outline-color:var(--mint)}
@media(max-width:850px){.workspace{grid-template-columns:minmax(0,1fr)}}
@media(max-width:600px){main{padding-top:12px}.workspace{gap:12px;margin:12px 0}
 header::before{left:30%;opacity:.5}header::after{width:36%;opacity:.42}
 .snapshot-row label,.snapshot-row select{max-width:100%}.snapshot-row label{display:grid;flex:1;min-width:0}
 .actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.actions button{padding-inline:5px}
 #weeklyPanel .row{align-items:flex-start}nav{gap:12px}}
@media(prefers-reduced-motion:reduce){button{transition:none}}
</style><main><header><div class="heading-line"><span class="brand-mark" aria-hidden="true"></span><h1>团队经营分析</h1><span id="identity"></span></div>
<nav aria-label="工作区导航"><a href="#output">结果与依据</a><a href="#sharedReports">分享的汇总</a><a href="#weeklyPanel">每周经营复盘</a></nav></header>
<div class="workspace"><div class="main-column">
<section id="analysis" hidden><h2>对话分析</h2><div class="row snapshot-row"><label>数据快照 <select id="dataset"></select></label>
<button id="fresh">新会话</button></div><label class="question-label" for="question">经营问题</label>
<select id="examples" aria-label="当前快照可问的问题"></select>
<textarea id="question" rows="2" placeholder="输入经营问题，或选择上方示例"></textarea>
<div class="actions"><button class="primary" id="ask">分析</button> <button id="sync">同步交付并重算</button> <button id="resume">继续未完成分析</button>
<button id="proof">本轮计算依据</button></div>
<div class="analysis-tools"><details><summary>历史会话</summary><div class="row"><input id="term" aria-label="搜索问题"><button id="search">搜索</button>
<button id="sessions">列出会话</button><select id="saved" aria-label="历史会话"></select><button id="open">打开</button></div></details>
<details><summary>比较与分享</summary><label>目标快照 <select id="target"></select></label> <button id="compare">比较</button>
<p><input id="title" placeholder="报告标题"><input id="recipient" placeholder="接收人账号"><button id="share">分享当前汇总</button>
<button id="export" hidden>下载完整结果</button></p></details>
<details id="orderTools" hidden><summary>核查订单</summary><input id="orderId" placeholder="订单号"> <button id="order">查看来源行</button></details>
<details id="sqlTools" hidden><summary>只读 SQL</summary><select id="scope"><option value="analysis_result">当前分析结果</option></select>
<textarea id="sqlText" placeholder="输入问题生成 SQL 草稿，或直接输入查询"></textarea><button id="draft">生成草稿</button> <button id="sql">执行</button></details></div>
</section>
<section id="output" aria-live="polite"><h2>结果与依据</h2><p id="status"></p><p class="empty-result">分析结果和已打开的报告将显示在这里。</p><div id="result"></div></section></div>
<aside aria-label="团队工具"><section id="sharedReports"><h2>分享的汇总</h2><p class="empty-list" id="noReports" hidden>暂无可查看的汇总。</p><select id="reports" aria-label="可查看的报告"></select> <button id="report">打开报告</button></section>
<details id="uploadTools" hidden><summary>接入数据</summary><label>模板 <select id="template"><option value="orders_items">订单和商品明细</option><option value="full">完整九表</option></select></label>
<p><input id="files" type="file" accept=".xlsx,.csv" multiple><button id="inspect">识别字段</button></p>
<div id="mappingFields"></div><label>轻量模板金额口径（人民币元）
<select id="amount"><option value="line">已确认行商品额，不含运费</option><option value="unit">已确认商品单价乘数量</option><option value="unknown">待确认，先统计订单</option></select></label>
<details><summary>预览来源样本与高级映射</summary><pre id="samples"></pre>
<textarea id="mapping" rows="10" aria-label="字段映射与别名"></textarea></details><input id="datasetName" placeholder="数据集名称">
<label>来源版本 <select id="parent"></select></label> <button id="preview">核对可用指标</button> <button id="import">导入</button></details>
<details id="claimTools" hidden><summary>认领本机旧会话</summary><label>当前快照的旧会话 ID <input id="legacyId"></label>
<button id="claim">认领到管理员历史</button></details>
<details id="jobPanel"><summary>后台任务</summary><button id="refreshJobs">刷新任务</button>
<p id="jobMessage" role="status"></p><div id="jobList"></div></details>
<details id="opsPanel" hidden><summary>实例运行状态</summary><button id="opsStatus">查看近 7 天</button>
<button id="modelProbe">测试模型连接</button><div id="opsSummary"></div><div id="opsDaily"></div></details>
</aside></div>""" + PANEL + """</main>
<script>
const el=id=>document.getElementById(id); let session=null,examples={},view=0,actionTail=Promise.resolve(),jobTimer;
function showExamples(){const qs=examples[el('dataset').value]||[];options(el('examples'),[['','选择一个可问的问题'],...qs.map(q=>[q,q])])}
function options(select,entries){select.replaceChildren(...entries.map(([k,v])=>new Option(v,k)))}
const fieldLabels={orders:'订单',order_items:'商品明细',order_payments:'支付',order_reviews:'评价',order_id:'订单号',order_item_id:'稳定明细键',
 purchase_at:'下单时间',order_status:'订单状态',price:'商品额或单价',quantity:'数量',freight_value:'行运费',category_id:'品类ID',
 category_name_zh:'品类名称',seller_id:'商家ID',seller_name:'商家名称',product_id:'商品ID',review_score:'评分',review_id:'评价ID',payment_value:'支付金额',payment_sequence:'支付序号'};
function editMapping(mapping,sources){
 const box=el('mappingFields');box.replaceChildren();const save=()=>{el('mapping').value=JSON.stringify(mapping,null,2)};
 for(const [table,spec] of Object.entries(mapping.tables)){
  const group=document.createElement('details');group.open=['orders','order_items'].includes(table);
  const title=document.createElement('summary');title.textContent=fieldLabels[table]||table;group.append(title);
  const select=document.createElement('select');select.setAttribute('aria-label',title.textContent+'来源表');
  options(select,[['','选择来源表'],...Object.keys(sources).map(k=>[k,k])]);
  select.value=spec.sheet?spec.file+':'+spec.sheet:spec.file;group.append(select);
  select.onchange=()=>{const src=sources[select.value];spec.file=src?.file||'';spec.sheet=src?.sheet||null;
   for(const key of Object.keys(spec.columns))spec.columns[key]=src?.columns.includes(key)?key:'';
   editMapping(mapping,sources)};
  const src=sources[select.value];
  for(const [field,current] of Object.entries(spec.columns)){
   const label=document.createElement('label');label.textContent=(fieldLabels[field]||field)+' ';
   const pick=document.createElement('select');options(pick,[['','未映射'],...(src?.columns||[]).map(c=>[c,c])]);
   pick.value=current;pick.onchange=()=>{spec.columns[field]=pick.value;editMapping(mapping,sources)};label.append(pick);group.append(label,document.createTextNode(' '))}
  if(mapping.template && table==='orders' && src){const status=spec.columns.order_status;
   for(const value of new Set(src.status_values?.[status]||src.sample.map(r=>r[status]).filter(v=>v!=null && v!==''))){
    const label=document.createElement('label');label.textContent='来源状态 '+value+' 对应 ';
    const pick=document.createElement('select');const states={delivered:'已送达',canceled:'已取消',shipped:'已发货',approved:'已确认',processing:'处理中',created:'已创建',invoiced:'已开票',unavailable:'缺货'};
    options(pick,[['','保留原状态'],...Object.entries(states)]);pick.value=mapping.status_map[value]||'';
    pick.onchange=()=>{if(pick.value)mapping.status_map[value]=pick.value;else delete mapping.status_map[value];save()};
    label.append(pick);group.append(document.createElement('br'),label)}}
  box.append(group)}
 save()
}
async function request(path,body){
 const init=body===undefined?{}:{method:'POST',headers:{'X-Analysis-Action':'1'},body};
 if(typeof body==='string')init.headers['Content-Type']='application/json';
 const r=await fetch(path,init);const data=await r.json();
 if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:JSON.stringify(data.detail));return data
}
async function refresh(selected){const d=await request('/api/home');el('identity').textContent=d.name;examples=d.examples;
 const previous=el('dataset').value;
 for(const id of ['dataset','target'])options(el(id),Object.entries(d.datasets));
 options(el('parent'),[['','新数据源'],...Object.entries(d.datasets)]);
 if(d.datasets[selected||previous])el('dataset').value=selected||previous;showExamples();
 options(el('reports'),d.reports.map(r=>[r.id,r.title]));
 el('reports').hidden=el('report').disabled=!d.reports.length;el('noReports').hidden=!!d.reports.length;
 el('analysis').hidden=!d.permissions.includes('analyze');el('uploadTools').hidden=d.role!=='admin';
 el('claimTools').hidden=d.role!=='admin';
 el('opsPanel').hidden=d.role!=='admin';el('jobPanel').hidden=d.role==='reader';
 el('weeklyAnalyst').hidden=!d.permissions.includes('analyze');el('weeklyIssues').hidden=!d.permissions.includes('details');
 el('weeklyCoverage').hidden=el('ledgerAdmin').hidden=d.role!=='admin';
 el('orderTools').hidden=!d.permissions.includes('details');el('sqlTools').hidden=!d.permissions.includes('sql');
 el('export').hidden=!d.permissions.includes('export');
 if(d.permissions.includes('details') && !el('scope').querySelector('[value="raw"]'))el('scope').add(new Option('原始表','raw'))
}
function show(data){const box=el('result');box.replaceChildren();const r=data.result||data;
 if(data.answer){const p=document.createElement('pre');p.className='answer';p.textContent=data.answer;box.append(p)}
 if(r.error){const p=document.createElement('pre');p.textContent=r.error;box.append(p)}
 if(r.columns && r.rows){const t=document.createElement('table');
 for(const [i,row] of [r.columns,...r.rows].entries()){const tr=t.insertRow();
 for(const v of row){const c=document.createElement(i?'td':'th');c.textContent=v??'未知';tr.append(c)}}box.append(t);
 if(r.quality?.length){const details=document.createElement('details'),title=document.createElement('summary'),quality=document.createElement('pre');
 title.textContent='金额准入与数据完整度';quality.textContent=JSON.stringify(r.quality,null,2);details.append(title,quality);box.append(details)}}
 const p=document.createElement('pre');const extra={...data};if(r.columns){delete extra.rows;delete extra.columns;delete extra.result}
 delete extra.answer;delete extra.download;
 if(Object.keys(extra).length){
  p.textContent=JSON.stringify(extra,null,2);
  if(r.columns && r.rows){
   const detail=document.createElement('details'),title=document.createElement('summary');
   title.textContent='查看完整记录';detail.append(title,p);box.append(detail)
  }else box.append(p)
 }
 if(data.download){const a=document.createElement('a');a.href='/api/files/'+encodeURIComponent(data.download);a.textContent='下载本次结果';box.append(a)}
 if(r.columns && r.rows){
  const output=el('output'),bounds=output.getBoundingClientRect();
  if(bounds.bottom<=0||bounds.top>innerHeight-80)output.scrollIntoView({block:'start'})
 }
}
function bind(id,fn){el(id).onclick=async()=>{el(id).disabled=true;el('status').textContent='处理中…';try{
 const result=await fn();el('status').textContent=result?.job?'已提交后台任务':result?.result?.error?'查询未完成':'完成'
 }catch(e){el('status').textContent=e.message}finally{el(id).disabled=false}}}
function act(action,value='',target='',saved=null){
 const selectedView=view,scope=el('scope').value;
 const operation=actionTail.then(async()=>{
  if(selectedView!==view)throw Error('会话已切换，请在当前会话重新提交');
  const data=await request('/api/action',JSON.stringify({action,value,target,dataset:el('dataset').value,session:saved||session,scope}));
  if(selectedView===view){
   if(data.session)session=data.session;
   if(data.dataset && data.dataset!==el('dataset').value)await refresh(data.dataset);
   if(data.job)await showJob(data);else show(data)
  }return data
 });actionTail=operation.catch(()=>{});return operation
}
function reset(){view++;session=null;el('result').replaceChildren();el('status').textContent='新会话';el('question').value=''}
el('dataset').onchange=()=>{reset();showExamples()};el('fresh').onclick=reset;
el('examples').onchange=()=>{el('question').value=el('examples').value};
bind('ask',()=>act('ask',el('question').value));bind('sync',()=>act('ask','数据更新了'));
bind('resume',()=>act('resume'));
bind('proof',()=>act('proof'));
bind('claim',()=>act('claim',el('legacyId').value));
bind('compare',()=>act('compare','',el('target').value));bind('order',()=>act('order',el('orderId').value));
bind('share',async()=>{await act('share',el('title').value,el('recipient').value);await refresh()});
bind('export',()=>act('queue_export'));bind('sql',()=>act('sql',el('sqlText').value));
bind('draft',async()=>{const d=await act('draft',el('sqlText').value);el('sqlText').value=d.result.sql});
for(const id of ['sessions','search'])bind(id,async()=>{const d=await act(id,el('term').value);options(el('saved'),(d.sessions||d.hits).map(s=>[s.session_id,s.question||s.title]))});
bind('open',()=>act('open','','',el('saved').value));
bind('report',async()=>show(await request('/api/reports/'+encodeURIComponent(el('reports').value))));
function currentMapping(){if(!el('mapping').value)throw Error('请先识别字段');const map=JSON.parse(el('mapping').value);if(map.template)map.amount_mode=el('amount').value;return map}
el('template').onchange=()=>{el('mapping').value='';el('mappingFields').replaceChildren();el('samples').textContent='';el('preview').hidden=el('template').value!=='orders_items'};
bind('preview',async()=>show(await request('/api/mapping/preview',JSON.stringify(currentMapping()))));
for(const id of ['inspect','import'])bind(id,async()=>{const f=new FormData();for(const file of el('files').files)f.append('files',file);
 const map=id==='import'?currentMapping():{};
 f.append('template',el('template').value);f.append('mapping',JSON.stringify(map));f.append('name',el('datasetName').value||'经营数据');f.append('parent',el('parent').value);
 const d=await request('/api/upload/'+(id==='import'?'queue':id),f);
 if(id==='inspect'){editMapping(d.mapping,d.samples);el('samples').textContent=JSON.stringify(d.samples,null,2);return}
 return showJob(d)});
const jobKind={import:'数据导入',export:'完整导出',weekly:'周报计算'};
const jobStatus={queued:'排队中',running:'执行中',failed:'失败',cancelled:'已取消',succeeded:'已完成'};
async function showJob(data){
 el('jobPanel').open=true;el('jobMessage').textContent=(data.reused?'复用已有任务 ':'任务已登记 ')+data.job;
 await refreshJobs();return data
}
function taskButton(title,fn){
 const button=document.createElement('button');button.textContent=title;button.onclick=async()=>{
  button.disabled=true;
  try{await fn()}catch(error){el('jobMessage').textContent=error.message}finally{button.disabled=false}
 };return button
}
async function refreshJobs(){
 clearTimeout(jobTimer);
 const {jobs}=await request('/api/jobs');
 const box=el('jobList');box.replaceChildren();
 if(!jobs.length)box.textContent='暂无任务';
 for(const j of jobs){
  const row=document.createElement('p');row.className='row';
  const completed=j.progress.filter(s=>s.report).length;
  const text=jobKind[j.kind]+' · '+jobStatus[j.status]+(j.cancelling&&j.status==='running'?'，本期结束后停止':'');
  row.append(document.createTextNode(text+(j.kind==='weekly'?' · 已完成 '+completed+' 期':'')+' · '+j.job.slice(0,8)));
  const action=op=>async()=>{await request('/api/jobs/'+j.job+'/'+op,'{}');await refreshJobs()};
  if(j.status==='queued'||j.status==='running'&&j.kind==='weekly'&&!j.cancelling)row.append(taskButton('取消',action('cancel')));
  if(j.status==='failed'||j.status==='cancelled')row.append(taskButton('重试',action('retry')));
  if(j.error)row.append(document.createTextNode(j.error));
  if(j.result?.dataset)row.append(taskButton('选择新快照',async()=>{await refresh(j.result.dataset);reset();show(j.result)}));
  if(j.result?.download){const link=document.createElement('a');link.href='/api/files/'+j.result.download;link.textContent='下载完整结果';row.append(link)}
  for(const step of j.progress){
   if(step.report){const link=document.createElement('a');link.href='/?weekly='+step.report;link.textContent='查看周报 '+step.report.slice(0,6);row.append(link)}
   if(step.pending)row.append(document.createTextNode('待重算 '+step.pending.slice(0,6)+' '+step.reason))
  }
  box.append(row)
 }
 if(jobs.some(j=>j.status==='queued'||j.status==='running'))jobTimer=setTimeout(
  ()=>refreshJobs().catch(error=>el('jobMessage').textContent=error.message),1500)
}
bind('refreshJobs',refreshJobs);
async function showOperations(){
 const d=await request('/api/operations'), summary=el('opsSummary'), table=document.createElement('table');
 const model=d.model.status==='configured'?'已配置 '+d.model.model:'配置有误';
 summary.textContent='网页在线 · 工作进程'+(d.worker==='online'?'在线':'离线')+' · 模型'+model+
  ' · 最近模型结果 '+(d.model_last_result?.status||'待验证')+
  ' · 最新快照 '+(d.latest_delivery?.dataset_id||'无')+' · 最近导入任务 '+(d.latest_import?jobStatus[d.latest_import.status]:'无');
 const labels={turn:'交互执行',turn_wait:'交互排队',model:'模型请求',model_probe:'模型探测',query:'只读查询',
  weekly_calculation:'周报计算',job_import:'导入',job_export:'导出',job_weekly:'周报任务',
  job_wait_import:'导入等待',job_wait_export:'导出等待',job_wait_weekly:'周报等待'};
 for(const [i,values] of [['日期（UTC）','操作','状态','次数','P50 毫秒','P95 毫秒','Token 合计','用量样本'],
  ...d.daily.map(x=>[x.day,labels[x.kind]||x.kind,jobStatus[x.status]||x.status,x.count,x.p50_ms,x.p95_ms,x.tokens,x.token_samples])].entries()){
  const row=table.insertRow();for(const value of values){const cell=document.createElement(i?'td':'th');cell.textContent=value??'—';row.append(cell)}
 }
 el('opsDaily').replaceChildren(table)
}
bind('opsStatus',showOperations);bind('modelProbe',async()=>{await request('/api/operations/probe','{}');await showOperations()});
""" + SCRIPT + """
(async()=>{await refresh();const key=new URLSearchParams(location.search).get('weekly');await weeklyIndex(key);
 if(key){if(el('weeklyReport').value!==key)throw Error('该周报不可访问');await weeklyView()}
 if(!el('jobPanel').hidden)await refreshJobs()})().catch(e=>el('status').textContent=e.message);
</script></html>"""
