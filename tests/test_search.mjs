// 搜尋核心測試：完全包含 → 字依序出現 → Fuse.js 相近名稱；不連網、使用 web/vendor/ 內的固定版本。
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const source=html.match(/  function searchRows\([\s\S]*?  \/\/ 搜尋核心結束。/)[0];
const plain=vm.createContext({});
vm.runInContext(source,plain);
const withFuse=vm.createContext({});
vm.runInContext(readFileSync(new URL('../web/vendor/fuse-7.0.0.basic.min.js',import.meta.url),'utf8'),withFuse);
vm.runInContext(source,withFuse);

const rows=[
  {code:'0050',name:'元大台灣50',issuer:'元大'},
  {code:'0056',name:'元大高股息',issuer:'元大'},
  {code:'00878',name:'國泰永續高股息',issuer:'國泰'},
  {code:'00662',name:'富邦NASDAQ',issuer:'富邦'},
  {code:'00646',name:'元大S&P500',issuer:'元大'},
];
const codes=r=>Array.from(r.rows,d=>d.code);

test('沒有搜尋字時原樣回傳',()=>{
  const r=plain.searchRows(rows,'');
  assert.equal(r.rows,rows);assert.equal(r.fuzzy,false);
});

test('代號、名稱、投信完全包含時只列完全相符，不標示相近',()=>{
  assert.deepEqual(codes(withFuse.searchRows(rows,'0056')),['0056']);
  assert.deepEqual(codes(withFuse.searchRows(rows,'高股息')),['0056','00878']);
  assert.deepEqual(codes(withFuse.searchRows(rows,'國泰')),['00878']);
  assert.deepEqual(codes(withFuse.searchRows(rows,'nasdaq')),['00662']);
  assert.equal(withFuse.searchRows(rows,'0056').fuzzy,false);
});

test('中文簡稱：字依序出現即列出，忽略空白，並標示為相近名稱',()=>{
  const r=plain.searchRows(rows,'高息');
  assert.deepEqual(codes(r),['0056','00878']);assert.equal(r.fuzzy,true);
  assert.deepEqual(codes(plain.searchRows(rows,'元大 50')),['0050','00646']);
  assert.deepEqual(codes(plain.searchRows(rows,'息高')),[]);
});

test('前兩步都沒有結果才用 Fuse.js，且只比對名稱與投信、不比對代號',()=>{
  const r=withFuse.searchRows(rows,'nasdac');
  assert.equal(r.fuzzy,true);assert.equal(r.rows[0].code,'00662');
  class Spy{constructor(list,options){Spy.list=list;Spy.options=options;}search(q){Spy.q=q;return [{item:Spy.list[2]}];}}
  const spied=plain.searchRows(rows,'zzz',Spy);
  assert.deepEqual(Array.from(Spy.options.keys,k=>k.name),['name','issuer']);
  assert.equal(Spy.q,'zzz');assert.deepEqual(codes(spied),['00878']);
  assert.equal(plain.searchRows(rows,'0056',Spy).fuzzy,false);
});

test('外掛沒載入時退回前兩步，不丟錯、不列相近名稱',()=>{
  assert.equal(typeof plain.Fuse,'undefined');
  const r=plain.searchRows(rows,'nasdac');
  assert.deepEqual(codes(r),[]);assert.equal(r.fuzzy,false);
  assert.deepEqual(codes(plain.searchRows(rows,'nasdq')),['00662']);
});

test('單一字不做依序比對，也不修改原始資料',()=>{
  const copy=JSON.stringify(rows);
  assert.deepEqual(codes(plain.searchRows(rows,'股')),['0056','00878']);
  assert.deepEqual(codes(plain.searchRows(rows,'q')),['00662']);
  assert.deepEqual(codes(plain.searchRows(rows,'z')),[]);
  assert.equal(JSON.stringify(rows),copy);
});
