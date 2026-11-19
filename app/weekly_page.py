PANEL = """<section id="weeklyPanel"><h2>每周经营复盘</h2>
<div class="row"><label>团队周报 <select id="weeklyReport"></select></label><button id="viewWeekly">查看周报</button>
<a id="weeklyLink" hidden>本周报固定链接</a></div>
<div id="weeklyAnalyst" hidden><div class="row"><label>模板 <select id="weeklyTemplate"></select></label>
<label>截至业务日期 <input id="weekAsOf" type="date"></label><button id="runWeekly">运行上周复盘</button></div>
<p>使用上方选定的数据快照。日期留空时采用模板业务时区的今天。</p>
<details><summary>批量运行周报</summary><label>截至业务日期（每行一个，最多 12 个）<textarea id="batchWeeks" rows="3" placeholder="2025-02-03"></textarea></label>
<button id="runBatch">提交批量复盘</button></details>
<details><summary>保存或调整模板</summary>
<p>先在上方会话完成一次总体汇总或比较，再保存当前口径。</p>
<label>模板名称 <input id="weekTitle"></label>
<label>指标 <select multiple id="weekMetrics"><option value="order_count">订单数</option><option value="sales">商品额</option><option value="freight">运费</option></select></label>
<label>订单状态 <input id="weekStatuses" placeholder="留空沿用当前分析"></label>
<label>时区 <input id="weekZone" value="Asia/Shanghai"></label><label>周起始日 <select id="weekDay"></select></label>
<label>比较方式 <select id="weekCompare"><option value="1">对前一周</option><option value="4">对四周前</option></select></label>
<label><input type="checkbox" id="weekCategory">列出品类贡献</label>
<label><input type="checkbox" id="weekRefunds">附两期退款和净商品额</label>
<button id="saveWeekly">新建模板</button> <button id="versionWeekly">保存为所选模板的新版</button></details>
<details><summary>发布与修订</summary><label>接收人账号 <input id="weekRecipient"></label>
<button id="publishWeekly">复核并发布所选周报</button><button id="reviseWeekly">在当前快照修订所选周报</button></details>
<details id="weeklyIssues" hidden><summary>订单核实单</summary><label>本报告的冲突明细 <select id="issueRow"></select></label>
<button id="issueSource">查看这笔订单的来源行</button><label>负责人 <input id="issueOwner"></label><button id="newIssue">建立核实单</button>
<p><select id="issueList" aria-label="核实单"></select> <button id="viewIssue">查看核实经过</button></p>
<label>跟进说明 <input id="issueNote"></label><label>预期交付批次 <input id="issueBatch"></label>
<button id="issueComment">追加跟进</button> <button id="issueCorrect">登记更正</button> <button id="issueClose">确认关闭</button></details>
<details id="weeklyCoverage" hidden><summary>确认完整交付</summary>
<label>期间起日 <input id="deliveryStart" type="date"></label><label>截止日（不含） <input id="deliveryEnd" type="date"></label>
<label>业务日期字段 <select id="deliveryField"><option value="purchase_at">下单</option><option value="approved_at">审批</option>
<option value="shipped_at">发货</option><option value="delivered_at">送达</option><option value="estimated_delivery_at">预计送达</option></select></label>
<button id="confirmWeekly">确认当前快照交付完整</button></details>
<details><summary>按退款入账期核算净商品额</summary><label>起日 <input id="netStart" type="date"></label>
<label>截止日（不含） <input id="netEnd" type="date"></label><label>财务净额（人民币，可选） <input id="financeNet"></label>
<button id="calcNet">核账</button><div id="ledgerAdmin" hidden><label>完整退款流水 CSV <input type="file" id="refundFile" accept=".csv"></label>
<label>入账时区 <input id="ledgerZone" value="Asia/Shanghai"></label><button id="importLedger">绑定流水并确认上述交付期间</button></div></details>
</div></section>"""

SCRIPT = """
const weeklyApi='/api/weekly', val=id=>el(id).value;
options(el('weekDay'),['一','二','三','四','五','六','日'].map((v,i)=>[i,'星期'+v]));
async function weeklyIndex(report,template,ticket){
 const d=await request(weeklyApi);
 for(const [id,items,wanted] of [
  ['weeklyReport',d.reports.map(r=>[r.id,r.title+' · '+r.week+(r.published?'（已发布）':'（草稿）')]),report],
  ['weeklyTemplate',d.templates.map(t=>[t.id,t.title+' v'+t.version]),template],
  ['issueList',d.tickets.map(t=>[t.id,t.order_id+' / '+t.item_key+' · '+t.state]),ticket]]){
  const pick=el(id),previous=wanted||pick.value;options(pick,items);if(items.some(i=>i[0]===previous))pick.value=previous}
}
async function weeklyView(){
 const key=val('weeklyReport');if(!key)throw Error('请选择周报');
 const r=await request(weeklyApi+'/reports/'+key);
 options(el('issueRow'),(r.conflicts||[]).map(c=>[JSON.stringify([key,c.order,c.item,r.dataset]),c.order+' / '+c.item+' · '+c.prices.join(' 或 ')]));
 el('weeklyLink').href='/?weekly='+key;el('weeklyLink').hidden=false;
 const labels={order_count:'订单数',sales:'商品额（元）',freight:'运费（元）'},v=r.overview[0];
 show({answer:r.title+' · '+(r.published?'已发布':'草稿')+' · 模板 v'+r.version,
  result:{columns:['指标','本周','对照周','变化'],rows:r.plan.metric_ids.map(m=>[labels[m],v[m+'_current'],v[m+'_previous'],v[m+'_change']]),quality:[v.quality]},
  '本周':r.plan.time_range,'对照周':r.plan.comparison_range,'日期口径':r.plan.time_field,'订单状态':r.plan.order_statuses,
  '筛选条件':{实体:r.plan.filters,订单:r.plan.order_where,商品:r.plan.item_where},'品类贡献':r.categories,'待核实明细数':r.price_conflict_count,
  '价格冲突':r.conflicts,'退款核算':r.refunds,'修订':r.revision,'原报告':r.parent,'依据':{报告:key,快照:r.dataset,快照指纹:r.snapshot,规则:r.rule,原分析:r.source_record,交付:r.coverage,查询:r.statements}});
 if(r.parent){const link=document.createElement('a');link.href='/?weekly='+r.parent;link.textContent='打开原周报';el('result').append(link)}
}
el('weeklyReport').onchange=()=>{el('weeklyLink').hidden=true;el('issueRow').replaceChildren()};
bind('viewWeekly',weeklyView);
async function saveTemplate(version){
 const metrics=Array.from(el('weekMetrics').selectedOptions,o=>o.value);
 const statuses=val('weekStatuses').trim();
 const d=await request(weeklyApi+'/templates',JSON.stringify({dataset:val('dataset'),session:session||'',title:val('weekTitle'),
 timezone:val('weekZone'),first_day:Number(val('weekDay')),compare_weeks:Number(val('weekCompare')),category:el('weekCategory').checked,
 metrics:metrics.length?metrics:null,net_refunds:el('weekRefunds').checked,statuses:statuses?statuses.split(/[,， ]+/):null,prior:version?val('weeklyTemplate'):null}));
 await weeklyIndex(null,d.template);show(d)
}
bind('saveWeekly',()=>saveTemplate(false));bind('versionWeekly',()=>saveTemplate(true));
async function computeWeekly(revise){
 const dates=[val('weekAsOf')||null];return queueWeeks(dates,revise?val('weeklyReport'):null)
}
async function queueWeeks(dates,revise=null){
 const weeks=dates.map(as_of=>({dataset:val('dataset'),template:val('weeklyTemplate'),as_of,revise}));
 return showJob(await request(weeklyApi+'/jobs',JSON.stringify({weeks})))
}
bind('runWeekly',()=>computeWeekly(false));bind('reviseWeekly',()=>computeWeekly(true));
bind('runBatch',()=>queueWeeks(val('batchWeeks').trim().split(/\\s+/).filter(Boolean)));
bind('publishWeekly',async()=>{const key=val('weeklyReport');await request(weeklyApi+'/reports/'+key+'/publish?recipient='+encodeURIComponent(val('weekRecipient')),'{}');
 await weeklyIndex(key);await weeklyView()});
bind('confirmWeekly',async()=>{const v=await request(weeklyApi+'/coverage?background=true',JSON.stringify({dataset:val('dataset'),field:val('deliveryField'),start:val('deliveryStart'),end:val('deliveryEnd')}));
 if(v.job)return showJob(v);await weeklyIndex();show(v)});
bind('newIssue',async()=>{if(!val('issueRow'))throw Error('请先打开带有价格冲突的周报，选择明细');const [report,order,item]=JSON.parse(val('issueRow'));
 const r=await request(weeklyApi+'/tickets',JSON.stringify({report,order,item,assignee:val('issueOwner')}));await weeklyIndex(null,null,r.ticket);await showIssue()});
bind('issueSource',async()=>{if(!val('issueRow'))throw Error('请先打开周报并选择冲突明细');
 const [,order,,dataset]=JSON.parse(val('issueRow'));show(await request('/api/action',JSON.stringify({action:'order',dataset,value:order})))});
async function showIssue(){show(await request(weeklyApi+'/tickets/'+val('issueList')))}
bind('viewIssue',showIssue);
for(const [id,action] of [['issueComment','comment'],['issueCorrect','correct'],['issueClose','close']])bind(id,async()=>{
 await request(weeklyApi+'/tickets/'+val('issueList'),JSON.stringify({action,note:val('issueNote'),batch:val('issueBatch')}));
 await weeklyIndex();await showIssue()});
bind('importLedger',async()=>{const file=el('refundFile').files[0];if(!file)throw Error('请选择退款 CSV');
 const f=new FormData();for(const [key,value] of Object.entries({dataset_id:val('dataset'),start:val('netStart'),end:val('netEnd'),timezone:val('ledgerZone'),file}))f.append(key,value);
 const data=await request(weeklyApi+'/refunds?background=true',f);show(data);if(data.job)return showJob(data)});
bind('calcNet',async()=>{const v=await request(weeklyApi+'/net',JSON.stringify({dataset:val('dataset'),start:val('netStart'),end:val('netEnd'),finance_total:val('financeNet')||null}));
 show({answer:'按退款入账期的净商品额',result:{columns:['币种','准入商品额','已归属商品退款','已确认部分净额','完整净额','待核实流水数'],
 rows:Object.entries(v.currencies).map(([currency,b])=>[currency,b.eligible_sales,b.known_goods_refunds,b.known_net,b.net,b.unverified_count])},'核算依据':v})});
"""
