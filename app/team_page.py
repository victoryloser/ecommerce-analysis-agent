PAGE = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>团队经营分析</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f3;color:#203731;font:16px/1.6 "Source Sans 3","Microsoft YaHei",system-ui,sans-serif}
main{max-width:970px;margin:32px auto;padding:0 20px}section,details{background:white;border:1px solid #d0d9d4;padding:16px 20px;margin:16px 0}
header,.row{display:flex;align-items:center;gap:12px;flex-wrap:wrap}header{justify-content:space-between}
button,input,select,textarea{font:inherit;padding:6px 10px;max-width:100%}button{cursor:pointer;background:#164d40;color:white;border:0;border-radius:4px}
textarea{width:100%}table{border-collapse:collapse}th,td{padding:5px 10px;border-bottom:1px solid #ddd;text-align:left}
pre{white-space:pre-wrap;overflow-wrap:anywhere}#output{overflow:auto}h1,h2{margin:.3em 0}
</style><main><header><h1>团队经营分析</h1><span id="identity"></span></header>
<section id="analysis" hidden><div class="row"><label>数据快照 <select id="dataset"></select></label>
<button id="fresh">新会话</button></div><label for="question">经营问题</label>
<select id="examples" aria-label="当前快照可问的问题"></select>
<textarea id="question" rows="2" placeholder="输入经营问题，或选择上方示例"></textarea>
<button id="ask">分析</button> <button id="sync">同步交付并重算</button> <button id="proof">本轮计算依据</button>
<details><summary>历史会话</summary><div class="row"><input id="term" aria-label="搜索问题"><button id="search">搜索</button>
<button id="sessions">列出会话</button><select id="saved" aria-label="历史会话"></select><button id="open">打开</button></div></details>
<details><summary>比较与分享</summary><label>目标快照 <select id="target"></select></label> <button id="compare">比较</button>
<p><input id="title" placeholder="报告标题"><input id="recipient" placeholder="接收人账号"><button id="share">分享当前汇总</button>
<button id="export" hidden>下载完整结果</button></p></details>
<details id="orderTools" hidden><summary>核查订单</summary><input id="orderId" placeholder="订单号"> <button id="order">查看来源行</button></details>
<details id="sqlTools" hidden><summary>只读 SQL</summary><select id="scope"><option value="analysis_result">当前分析结果</option></select>
<textarea id="sqlText" placeholder="输入问题生成 SQL 草稿，或直接输入查询"></textarea><button id="draft">生成草稿</button> <button id="sql">执行</button></details>
</section>
<section><h2>分享的汇总</h2><select id="reports" aria-label="可查看的报告"></select> <button id="report">打开报告</button></section>
<details id="uploadTools" hidden><summary>接入数据</summary><label>模板 <select id="template"><option value="orders_items">订单和商品明细</option><option value="full">完整九表</option></select></label>
<p><input id="files" type="file" accept=".xlsx,.csv" multiple><button id="inspect">识别字段</button></p>
<div id="mappingFields"></div><label>轻量模板金额口径（人民币元）
<select id="amount"><option value="line">已确认行商品额，不含运费</option><option value="unit">已确认商品单价乘数量</option><option value="unknown">待确认，先统计订单</option></select></label>
<details><summary>预览来源样本与高级映射</summary><pre id="samples"></pre>
<textarea id="mapping" rows="10" aria-label="字段映射与别名"></textarea></details><input id="datasetName" placeholder="数据集名称">
<label>来源版本 <select id="parent"></select></label> <button id="preview">核对可用指标</button> <button id="import">导入</button></details>
<details id="claimTools" hidden><summary>认领本机旧会话</summary><label>当前快照的旧会话 ID <input id="legacyId"></label>
<button id="claim">认领到管理员历史</button></details>
<section id="output" aria-live="polite"><h2>结果与依据</h2><p id="status"></p><div id="result"></div></section></main>
<script>
const el=id=>document.getElementById(id); let session=null,examples={};
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
   for(const value of new Set(src.status_values?.[status]||src.sample.map(r=>r[status]).filter(Boolean))){
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
 el('analysis').hidden=!d.permissions.includes('analyze');el('uploadTools').hidden=d.role!=='admin';
 el('claimTools').hidden=d.role!=='admin';
 el('orderTools').hidden=!d.permissions.includes('details');el('sqlTools').hidden=!d.permissions.includes('sql');
 el('export').hidden=!d.permissions.includes('export');
 if(d.permissions.includes('details') && !el('scope').querySelector('[value="raw"]'))el('scope').add(new Option('原始表','raw'))
}
function show(data){const box=el('result');box.replaceChildren();const r=data.result||data;
 if(data.answer){const p=document.createElement('pre');p.textContent=data.answer;box.append(p)}
 if(r.columns && r.rows){const t=document.createElement('table');
 for(const [i,row] of [r.columns,...r.rows].entries()){const tr=t.insertRow();
 for(const v of row){const c=document.createElement(i?'td':'th');c.textContent=v??'未知';tr.append(c)}}box.append(t);
 if(r.quality?.length){const details=document.createElement('details'),title=document.createElement('summary'),quality=document.createElement('pre');
 title.textContent='金额准入与数据完整度';quality.textContent=JSON.stringify(r.quality,null,2);details.append(title,quality);box.append(details)}}
 const p=document.createElement('pre');const extra={...data};if(r.columns){delete extra.rows;delete extra.columns;delete extra.result}
 delete extra.answer;delete extra.download;if(Object.keys(extra).length){p.textContent=JSON.stringify(extra,null,2);box.append(p)}
 if(data.download){const a=document.createElement('a');a.href='/api/files/'+encodeURIComponent(data.download);a.textContent='下载本次结果';box.append(a)}
}
function bind(id,fn){el(id).onclick=async()=>{el(id).disabled=true;el('status').textContent='处理中…';try{await fn();el('status').textContent='完成'}catch(e){el('status').textContent=e.message}finally{el(id).disabled=false}}}
async function act(action,value='',target=''){const data=await request('/api/action',JSON.stringify({action,value,target,dataset:el('dataset').value,session,scope:el('scope').value}));
 if(data.session)session=data.session;if(data.dataset && data.dataset!==el('dataset').value)await refresh(data.dataset);show(data);return data}
function reset(){session=null;el('result').replaceChildren();el('status').textContent='新会话';el('question').value=''}
el('dataset').onchange=()=>{reset();showExamples()};el('fresh').onclick=reset;
el('examples').onchange=()=>{el('question').value=el('examples').value};
bind('ask',()=>act('ask',el('question').value));bind('sync',()=>act('ask','数据更新了'));
bind('proof',()=>act('proof'));
bind('claim',()=>act('claim',el('legacyId').value));
bind('compare',()=>act('compare','',el('target').value));bind('order',()=>act('order',el('orderId').value));
bind('share',async()=>{await act('share',el('title').value,el('recipient').value);await refresh()});
bind('export',()=>act('export'));bind('sql',()=>act('sql',el('sqlText').value));
bind('draft',async()=>{const d=await act('draft',el('sqlText').value);el('sqlText').value=d.result.sql});
for(const id of ['sessions','search'])bind(id,async()=>{const d=await act(id,el('term').value);options(el('saved'),(d.sessions||d.hits).map(s=>[s.session_id,s.question||s.title]))});
bind('open',async()=>{session=el('saved').value;await act('open')});
bind('report',async()=>show(await request('/api/reports/'+encodeURIComponent(el('reports').value))));
function currentMapping(){if(!el('mapping').value)throw Error('请先识别字段');const map=JSON.parse(el('mapping').value);if(map.template)map.amount_mode=el('amount').value;return map}
el('template').onchange=()=>{el('mapping').value='';el('mappingFields').replaceChildren();el('samples').textContent='';el('preview').hidden=el('template').value!=='orders_items'};
bind('preview',async()=>show(await request('/api/mapping/preview',JSON.stringify(currentMapping()))));
for(const id of ['inspect','import'])bind(id,async()=>{const f=new FormData();for(const file of el('files').files)f.append('files',file);
 const map=id==='import'?currentMapping():{};
 f.append('template',el('template').value);f.append('mapping',JSON.stringify(map));f.append('name',el('datasetName').value||'经营数据');f.append('parent',el('parent').value);
 const d=await request('/api/upload/'+id,f);if(id==='inspect'){editMapping(d.mapping,d.samples);el('samples').textContent=JSON.stringify(d.samples,null,2)}else{await refresh(d.dataset);reset();show(d)}});
refresh().catch(e=>el('status').textContent=e.message);
</script></html>"""
