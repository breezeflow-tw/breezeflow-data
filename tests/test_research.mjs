import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8'),context=vm.createContext({});
vm.runInContext(html.match(/  function researchDate\([\s\S]*?  \/\/ 十年研究計算核心結束。/)[0],context);
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
