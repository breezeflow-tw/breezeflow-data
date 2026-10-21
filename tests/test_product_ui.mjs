import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const context=vm.createContext({});
vm.runInContext(html.match(/  function profileOf\([\s\S]*?  \/\/ 商品與口徑核心結束。/)[0],context);

test('固定、上限、分段及原文四種語意分開',()=>{
  assert.equal(context.feeDisplay({mgmt_fee_text:'每年基金淨資產價值之 1%'}).label,'1%');
  assert.equal(context.feeDisplay({mgmt_fee_text:'最高為 1.00%'}).label,'最高 1%');
  assert.equal(context.feeDisplay({mgmt_fee_text:'0.35'}).label,'詳費率原文');
  assert.equal(context.feeDisplay({mgmt_fee_text:'200億元以下 1.2%；逾200億元 1.0%'}).kind,'tiered');
  assert.equal(context.feeDisplay({mgmt_fee_text:'上一年1%，今年0.5%'}).kind,'text');
});
test('已公布數值不被分段原文蓋掉，上限仍保留語意',()=>{
  assert.equal(context.feeDisplay({mgmt_fee:.1,mgmt_fee_text:'200億元以下 0.2%；逾200億元 0.1%'}).label,'0.1%');
  assert.equal(context.feeDisplay({mgmt_fee:1,mgmt_fee_text:'最高1%'}).kind,'cap');
  assert.equal(context.feeDisplay({mgmt_fee:Infinity}).label,'—');
  assert.equal(context.feeDisplay({mgmt_fee:0}).value,0);
});
test('核對狀態空值和無價格都計入，合計完整',()=>{
  const result=context.quoteCounts([{price:10,price_check:'一致'},{price:5},{price:null},{price:5,price_check:'未查核'}]);
  assert.equal(Object.values(result).reduce((a,b)=>a+b,0),4);
  assert.equal(result['狀態未提供'],1);assert.equal(result['無市價'],1);
});
test('資產與主動管理可同時篩選，未知分類不混入股票',()=>{
  const d={product:{asset_class:'債券',management:'主動式',trading_currency:'TWD',strategy:'非投資等級債'}};
  assert.equal(context.matchesProduct(d,{type:'債券',management:'主動式',strategy:'非投資等級債'}),true);
  assert.equal(context.matchesProduct(d,{type:'股票'}),false);
  assert.equal(context.matchesProduct({}, {type:'股票'}),false);
  assert.equal(context.matchesProduct({}, {type:'待確認'}),true);
});
test('同日規模需日期及新臺幣一致，月統計不冒充日資料',()=>{
  const d={aum:20,aum_date:'2026-10-21',product:{aum_currency:'TWD'}};
  assert.equal(context.matchesProduct(d,{aumMode:'same'},'2026-10-21'),true);
  for(const change of [{aum_date:null},{aum_date:'2026-09-30'},{aum_source:'SITCA月統計'},{product:{aum_currency:'USD'}},{aum:null}])
    assert.equal(context.matchesProduct({...d,...change},{aumMode:'same'},'2026-10-21'),false);
});
test('平均折溢價只納同日同幣別正式淨值，預估與跨日排除',()=>{
  const d={premium:1,nav_date:'2026-10-21',nav_source:'投信正式淨值',product:{trading_currency:'TWD',nav_currency:'TWD'}};
  assert.equal(context.formalComparable(d,'2026-10-21'),true);
  for(const change of [{nav_date:null},{nav_date:'2026-10-20'},{nav_source:'預估淨值'},{premium:Infinity},{product:{trading_currency:'USD',nav_currency:'TWD'}}])
    assert.equal(context.formalComparable({...d,...change},'2026-10-21'),false);
});
