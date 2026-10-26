// 資料完整度只描述已提供欄位，不能將缺漏當零或將行情日充當公告日。
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const source=html.match(/  function dateState\([\s\S]*?  \/\/ 日期與完整度結束。/)[0];
const context=vm.createContext({});
vm.runInContext(source,context);
const day=(value,source='')=>context.dateState(value,'2026-10-19',source);
const coverage=items=>context.fieldCoverage(items,'2026-10-19');

test('未提供與無效日期不使用行情日補值',()=>{
  for(const value of [null,undefined,'','2026-02-30','2026-13-01','2026-13'])
    assert.ok(['missing','invalid'].includes(day(value).kind));
});
test('同日、較早日與未來公告分開標示',()=>{
  assert.equal(day('2026-10-19').kind,'current');
  assert.equal(day('2026-10-16').kind,'earlier');
  assert.equal(day('2026-10-20').kind,'future');
  assert.equal(day('2026-11').kind,'future');
});
test('月公告不當成過期日資料，包含以月底日期表示的公會月統計',()=>{
  assert.equal(day('2026-09').kind,'monthly');
  assert.equal(day('2026-09-30','投信投顧公會（SITCA）月統計').kind,'monthly');
});
test('只有有效數值、日期和來源齊備才計入齊備檔數',()=>{
  const r=coverage([
    {nav:20,nav_date:'2026-10-16',nav_source:'投信官網正式淨值'},
    {nav:20,nav_source:'投信官網正式淨值'},
    {nav:20,nav_date:'2026-10-19'},
    {nav:20,nav_date:'2026-10-20',nav_source:'投信官網正式淨值'},
    {nav:0},{nav:null},{nav:'20'},{nav:Infinity}
  ])[0];
  assert.equal(r.present,4);assert.equal(r.complete,1);assert.equal(r.earlier,1);
});
test('持股空陣列不算有資料；月持股與月規模分開統計',()=>{
  const r=coverage([{holdings_top:[],holdings_date:'2026-09',holdings_source:'公會'},
    {holdings_top:[{asset_name:'測試股票'}],holdings_date:'2026-09',holdings_source:'公會',
     aum:10,aum_date:'2026-09-30',aum_source:'SITCA 月統計'}]);
  assert.equal(r[3].present,1);assert.equal(r[3].complete,1);assert.equal(r[3].monthly,1);
  assert.equal(r[1].monthly,1);assert.equal(r[1].earlier,0);
});
test('列表精簡檔只帶持股筆數，統計結果與完整資料相同',()=>{
  const base={holdings_date:'2026-10-19',holdings_source:'投信官網'};
  const full=coverage([{...base,holdings_top:[{asset_name:'甲'},{asset_name:'乙'}]},{...base,holdings_top:[]},{...base,holdings_top:null}]);
  const slim=coverage([{...base,holdings_count:2},{...base,holdings_count:0},{...base,holdings_count:0}]);
  assert.deepEqual(JSON.parse(JSON.stringify(slim[3])),JSON.parse(JSON.stringify(full[3])));
  assert.equal(slim[3].present,1);assert.equal(slim[3].complete,1);
});
test('空資料及篩選集合可正常統計，不修改輸入',()=>{
  assert.ok(Array.from(coverage([])).every(r=>r.present===0&&r.complete===0));
  const items=[{nav:20,nav_date:'2026-10-19',nav_source:'投信'}],before=JSON.stringify(items);
  assert.equal(coverage(items)[0].complete,1);assert.equal(JSON.stringify(items),before);
});
