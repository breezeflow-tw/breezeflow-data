// 排行榜純資料處理測試；不連網、不需要第三方套件。
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const source=html.match(/  function rankReturns\([\s\S]*?  \/\/ 排行核心結束。/)[0];
const context=vm.createContext({});
vm.runInContext(source,context);
const rank=(items,period='1d')=>context.rankReturns(items,period);
const item=(code,value)=>({code,price:20,change_pct:value});
const values=rows=>Array.from(rows,r=>r.value);

test('整頁內嵌 JavaScript 可正常解析',()=>{
  const scripts=Array.from(html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi),m=>m[1]);
  assert.ok(scripts.length>0);
  for(const script of scripts)assert.doesNotThrow(()=>new vm.Script(script));
});

test('正榜由高到低、負榜由最負到較負，各取十檔',()=>{
  const result=rank(Array.from({length:31},(_,i)=>item(String(i).padStart(4,'0'),i-15)));
  assert.deepEqual(values(result.positive),[15,14,13,12,11,10,9,8,7,6]);
  assert.deepEqual(values(result.negative),[-15,-14,-13,-12,-11,-10,-9,-8,-7,-6]);
  assert.equal(result.flat,1);
});
test('缺值、非數字、非有限數字及零市價均排除，漲跌幅零仍是有效持平',()=>{
  const data=[item('0',0),item('1',null),item('2',undefined),item('3','3'),item('4',NaN),item('5',Infinity),{...item('6',10),price:0},{...item('7',10),price:null}];
  const r=rank(data);
  assert.equal(r.valid,1);assert.equal(r.flat,1);assert.equal(r.missing,7);
  assert.equal(r.positive.length+r.negative.length,0);
});
test('週月取市價報酬，不混用日漲跌或含息報酬',()=>{
  const data=[{...item('0050',1),returns:{price:{'1w':2,'1m':-3},total:{'1w':99,'1m':99}}},item('0056',5)];
  assert.deepEqual(values(rank(data,'1w').positive),[2]);
  assert.deepEqual(values(rank(data,'1m').negative),[-3]);
  assert.equal(rank(data,'1m').missing,1);
});
test('相同報酬以代號排序，結果不受輸入順序影響',()=>{
  const data=[item('0056',2),item('0050',2),item('00900',-2),item('00878',-2)];
  const r=rank(data);
  assert.deepEqual(Array.from(r.positive,x=>x.item.code),['0050','0056']);
  assert.deepEqual(Array.from(r.negative,x=>x.item.code),['00878','00900']);
});
test('共同比例尺採正負兩側最大絕對值',()=>{
  const r=rank([item('a',2),item('b',-8)]);
  assert.equal(r.scale,8);assert.equal(r.positive[0].value/r.scale,0.25);
});
test('少於十檔與空資料不補位，不產生無限比例尺',()=>{
  const r=rank([item('a',1)]);
  assert.equal(r.positive.length,1);assert.equal(r.negative.length,0);
  const empty=rank([]);assert.equal(empty.scale,0);assert.equal(empty.valid,0);
});
test('只依傳入的篩選集合計算，不修改原始資料或順序',()=>{
  const data=[item('b',1),item('a',2)];const copy=JSON.stringify(data);
  assert.deepEqual(Array.from(rank(data.slice(0,1)).positive,x=>x.item.code),['b']);
  rank(data);assert.equal(JSON.stringify(data),copy);
});
