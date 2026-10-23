import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8'),context=vm.createContext({});
vm.runInContext(html.match(/  function researchDate\([\s\S]*?  \/\/ 十年研究計算核心結束。/)[0],context);
vm.runInContext(html.match(/  const fmt=[^\n]+/)[0]+'\n'+html.match(/  const esc=[^\n]+/)[0]+'\n'+html.slice(html.indexOf('  function researchChart('),html.indexOf('  function renderResearch(')),context);
const fixture=JSON.parse(readFileSync(new URL('./fixtures/research_package.json',import.meta.url),'utf8'));
function data(){return {required_start:'2026-10-19',as_of:'2026-10-22',listed_date:'2003-06-30',currency:'TWD',bars:structuredClone(fixture.prices),actions:structuredClone(fixture.actions),issues:[],unknown_calendar_dates:[],missing_price_dates:[],missing_action_dates:[],ready_ranges:[{start:'2026-10-19',end:'2026-10-22'}]};}
const opts={start:'2026-10-19',end:'2026-10-22',mode:'single',dividends:'cash',initial:100,monthly:0,payday:6,feeRate:0,minimum:0};

test('配息按發放日入帳，不重複加總；除息後買入無領息權',()=>{
  const result=context.simulateResearch(data(),opts);
  assert.equal(result.assets,100);assert.equal(result.received,20);assert.equal(result.cash,20);assert.equal(result.shares,10);
  assert.equal(result.ledger.find(r=>r.event==='配息入帳').date,'2026-10-21');
  const later=context.simulateResearch(data(),{...opts,start:'2026-10-20'});assert.equal(later.received,0);
});
test('再投入只能在發放後；尚未發放配息列應收',()=>{
  const result=context.simulateResearch(data(),{...opts,dividends:'reinvest'});
  assert.equal(result.shares,12);assert.equal(result.cash,4);assert.equal(result.assets,100);
  assert.equal(result.ledger.filter(r=>r.event==='模擬買入').at(-1).date,'2026-10-21');
  const pending=context.simulateResearch(data(),{...opts,end:'2026-10-20'});assert.equal(pending.receivable,20);assert.equal(pending.received,0);assert.equal(pending.assets,100);
});
test('分割改單位數，資產不會憑空翻倍；畸零反分割拒絕',()=>{
  const d=data();d.actions=[{date:'2026-10-20',kind:'split',ratio:2}];d.bars.slice(1).forEach(b=>{b.close=5;});
  const result=context.simulateResearch(d,opts);assert.equal(result.shares,20);assert.equal(result.assets,100);
  d.actions[0].ratio=.5;assert.throws(()=>context.simulateResearch(d,{...opts,initial:30}),/畸零/);
});
test('扣款日遇週末順延；超過結束日不扣款',()=>{
  const d=data();Object.assign(d,{required_start:'2026-10-30',as_of:'2026-11-03',ready_ranges:[{start:'2026-10-30',end:'2026-11-03'}],actions:[],bars:['2026-10-30','2026-11-02','2026-11-03'].map(date=>({date,close:10,open:10,high:10,low:10,volume:100}))});
  const result=context.simulateResearch(d,{...opts,start:d.required_start,end:d.as_of,mode:'monthly',initial:0,monthly:100,payday:31});
  assert.equal(result.contributed,100);assert.equal(result.ledger[0].date,'2026-11-02');
  assert.throws(()=>context.simulateResearch(d,{...opts,start:d.required_start,end:'2026-10-30',mode:'monthly',monthly:100,payday:31}),/沒有符合/);
});
test('費用包含在投入預算，最低費用與比例費用皆正確',()=>{
  const d=data();d.actions=[];d.bars.forEach(b=>b.close=10);
  let r=context.simulateResearch(d,{...opts,feeRate:.1});assert.equal(r.shares,9);assert.equal(r.fees,9);assert.equal(r.assets,91);
  r=context.simulateResearch(d,{...opts,minimum:5});assert.equal(r.fees,5);assert.equal(r.assets,95);
});
test('資料缺漏、公司行動未核實與不合法輸入必須擋住',()=>{
  for(const field of ['missing_price_dates','unknown_calendar_dates','missing_action_dates']){const d=data();d[field]=['2026-10-20'];assert.throws(()=>context.simulateResearch(d,opts));}
  for(const changes of [{start:'2016-01-01'},{initial:NaN},{payday:1.5},{feeRate:-.1},{mode:'unknown'},{start:'2026-02-30'}])assert.throws(()=>context.simulateResearch(data(),{...opts,...changes}));
});
test('XIRR 使用實際日數，未滿一年不年化',()=>{
  assert.equal(context.researchXirr([{date:'2025-01-01',amount:-100},{date:'2025-02-01',amount:110}]),null);
  assert.ok(Math.abs(context.researchXirr([{date:'2025-01-01',amount:-100},{date:'2026-01-01',amount:110}])-.1)<1e-10);
  assert.ok(Math.abs(context.researchXirr([{date:'2025-01-01',amount:-100},{date:'2025-07-01',amount:-100},{date:'2026-01-01',amount:200}]))<1e-10);
});
test('週月使用第一開、最高、最低、最後收及總量，缺日不彙整',()=>{
  const d=data(),week=context.aggregateResearch(d,'week');assert.equal(week.length,1);assert.equal(week[0].open,10);assert.equal(week[0].close,8);assert.equal(week[0].high,11);assert.equal(week[0].low,7);assert.equal(week[0].volume,4000);assert.equal(week[0].partial,true);
  d.missing_price_dates=['2026-10-20'];assert.equal(context.aggregateResearch(d,'week').length,0);
});
test('十年邊界正確處理閏年',()=>{
  assert.equal(context.researchYears('2024-02-29',10),'2014-02-28');assert.equal(context.researchYears('2024-02-29',20),'2004-02-29');
});
test('零頭不會在非扣款日因價格下跌而自動買入',()=>{
  const d=data();d.actions=[];d.bars[0].close=60;d.bars.slice(1).forEach(b=>b.close=20);
  const r=context.simulateResearch(d,opts);assert.equal(r.shares,1);assert.equal(r.cash,40);assert.equal(r.ledger.filter(x=>x.event==='模擬買入').length,1);
});
test('十年定期投入的現金與資產核算守恆',()=>{
  const d=data();Object.assign(d,{required_start:'2016-10-22',as_of:'2026-10-22',ready_ranges:[{start:'2016-10-22',end:'2026-10-22'}],actions:[]});
  d.bars=context.researchDays(d.required_start,d.as_of).filter(day=>![0,6].includes(new Date(day).getUTCDay())).map(date=>({date,close:10,open:10,high:10,low:10,volume:1000}));
  const r=context.simulateResearch(d,{...opts,start:d.required_start,end:d.as_of,mode:'combined',monthly:1000});
  assert.equal(r.assets,r.contributed);assert.equal(r.assets,r.marketValue+r.cash+r.receivable);assert.ok(Math.abs(r.xirr)<1e-10);assert.equal(r.contributed,120100);
});

test('小螢幕與特大字的 K 線保留有限座標，只有一根 K 棒進入 Tab 順序',()=>{
  const chart=context.researchChart(data(),'day',320,1.25);
  assert.match(chart,/viewBox="0 0 320 350"/);
  assert.doesNotMatch(chart,/NaN|Infinity/);
  assert.equal((chart.match(/tabindex="0"/g)||[]).length,1);
  assert.equal((chart.match(/role="button"/g)||[]).length,4);
  assert.match(chart,/開 8，高 9，低 7，收 8，成交量 1000 單位/);
  const flat=data();flat.bars.forEach(b=>Object.assign(b,{open:8,high:8,low:8,close:8,volume:0}));
  assert.doesNotMatch(context.researchChart(flat,'day',320,1.25),/NaN|Infinity/);
});

test('週線讀值包含真正末日與未結束標示；缺漏期間顯示空狀態',()=>{
  const d=data(),bar=context.aggregateResearch(d,'week')[0];
  assert.match(context.researchChart(d,'week'),/2026-10-19 至 2026-10-22/);
  assert.match(context.researchReadout(bar),/2026-10-19 ～ 2026-10-22/);
  assert.match(context.researchReadout(bar),/本期未結束/);
  assert.match(context.researchReadout(bar),/4,000/);
  d.missing_price_dates=['2026-10-20'];
  assert.doesNotMatch(context.researchChart(d,'week'),/<svg/);
  assert.match(context.researchChart(d,'week'),/資料還不完整/);
});

test('字型偏好僅接受有效選項，儲存不可用時不阻擋頁面',()=>{
  const boot=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(x=>x[1]).find(s=>s.includes('breezeflow-text-size'));
  for(const stored of ['large','extra','unexpected',null]){
    const document={documentElement:{dataset:{}}};
    vm.runInNewContext(boot,{document,localStorage:{getItem:()=>stored}});
    assert.equal(document.documentElement.dataset.textSize,['large','extra'].includes(stored)?stored:undefined);
  }
  assert.doesNotThrow(()=>vm.runInNewContext(boot,{document:{documentElement:{dataset:{}}},localStorage:{getItem(){throw Error('禁止儲存');}}}));
});

test('配息與分割尚未補齊仍可看原始日 K，空白日 K 不引導切回自己',()=>{
  const d=data();d.missing_action_dates=['2026-10-19'];d.ready_ranges=[];
  assert.match(context.researchChart(d,'day'),/<svg/);
  assert.throws(()=>context.simulateResearch(d,opts));
  d.bars=[];
  const empty=context.researchChart(d,'day');
  assert.match(empty,/尚未取得完整日 K 資料/);
  assert.doesNotMatch(empty,/切回日 K/);
});

test('自動載入歷史僅更新目前 ETF，切換時過期回應不覆寫新頁',async()=>{
  const loaded=[],holder={isConnected:true,setAttribute(){}};
  let release;
  const pending=new Promise(resolve=>release=resolve);
  const c=vm.createContext({Map,RegExp,$:()=>holder,detailDate:'2026-10-22',detailCode:'0050',
    fetch:async url=>url.endsWith('index.json')?{ok:true,json:async()=>({as_of:'2026-10-22',items:{'0050':{file:'0050-0123456789abcdef.json'}}})}:pending,
    renderResearch:(_holder,d)=>loaded.push(d.code)});
  vm.runInContext(html.slice(html.indexOf('  const researchCache='),html.indexOf('  function researchChart(')),c);
  const loading=c.loadResearch({code:'0050'});
  c.detailCode='00984A';
  release({ok:true,json:async()=>({version:1,as_of:'2026-10-22',code:'0050'})});
  await loading;
  assert.deepEqual(loaded,[]);
  c.detailCode='0050';
  await c.loadResearch({code:'0050'});
  assert.deepEqual(loaded,['0050']);
});
