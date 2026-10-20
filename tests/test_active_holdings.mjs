// 持股動向的離線前端測試。金額、股數、檔數屬不同口徑，不能互相補值。
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const source=html.match(/  function rankActiveHoldings\([\s\S]*?  \/\/ 持股排行核心結束。/)[0];
const context=vm.createContext({});
vm.runInContext(source,context);
const rank=(stocks,metric='amount',query='')=>context.rankActiveHoldings({stocks},metric,query);
const stock=(code,amount,buy=1,sell=0)=>({asset_code:code,asset_name:'測試股票',net_amount:amount,buy_etfs:buy,sell_etfs:sell});
const values=rows=>Array.from(rows,r=>r.value);
const codes=rows=>Array.from(rows,r=>r.stock.asset_code);

test('淨估值由最正和最負各取十名，不從預先截斷的加減碼清單排序',()=>{
  const result=rank(Array.from({length:41},(_,i)=>stock(String(i).padStart(4,'0'),i-20)));
  assert.deepEqual(values(result.positive),[20,19,18,17,16,15,14,13,12,11]);
  assert.deepEqual(values(result.negative),[-20,-19,-18,-17,-16,-15,-14,-13,-12,-11]);
  assert.equal(result.flat,1);
});
test('金額 null、非數字及無窮值不當作零，零估值單獨統計',()=>{
  const result=rank([null,stock('a',null),stock('b','5'),stock('c',NaN),stock('d',Infinity),stock('e',0)]);
  assert.equal(result.matched,5);
  assert.equal(result.missing,4);
  assert.equal(result.flat,1);
  assert.equal(result.positive.length+result.negative.length,0);
});
test('沒有估值仍可按已確認的檔數排名，同股可同時被不同 ETF 增減持',()=>{
  const result=rank([stock('a',null,3,2),stock('b',null,1,4)],'breadth');
  assert.deepEqual(values(result.positive),[3,1]);
  assert.deepEqual(values(result.negative),[-4,-2]);
  assert.equal(result.missing,0);
});
test('檔數只接受非負整數，不從金額正負推算檔數',()=>{
  const result=rank([stock('a',5,null,0),stock('b',5,-1,0),stock('c',5,1.2,0),stock('d',5,0,0)],'breadth');
  assert.equal(result.missing,3);
  assert.equal(result.flat,1);
  assert.equal(result.positive.length,0);
});
test('部分 ETF 股數被排除時不拿殘缺檔數排名；僅缺價時仍保留完整檔數榜',()=>{
  const partial={...stock('a',10,5,2),complete:false,excluded_etfs:[{code:'00981A',reason:'缺股數'}]};
  const noPrice={...stock('b',null,3,1),complete:false,missing_price_dates:['2026-10-20'],excluded_etfs:[]};
  assert.deepEqual(codes(rank([partial,noPrice],'breadth').positive),['b']);
  assert.equal(rank([partial,noPrice],'breadth').missing,1);
  assert.equal(rank([partial,noPrice]).positive.length,0);
  assert.equal(rank([{...stock('c',null,9,0),counts_complete:false}],'breadth').positive.length,0);
});
test('長條共同比例尺取兩榜最大幅度',()=>{
  assert.equal(rank([stock('a',2),stock('b',-8)]).scale,8);
  assert.equal(rank([stock('a',2,3,1),stock('b',-8,1,7)],'breadth').scale,7);
});
test('淨股數和淨估值可異號，各排行榜使用自身口徑',()=>{
  const s={...stock('a',-0.5,2,0),net_shares:1000};
  assert.deepEqual(codes(rank([s]).negative),['a']);
  assert.deepEqual(codes(rank([s],'breadth').positive),['a']);
});
test('依股票代號或名稱搜尋，不修改原始集合；相同數值以代號穩定排序',()=>{
  const data=[{...stock('2330',2),asset_name:'台積電'},stock('1101',2),{...stock('2317',2),asset_name:'鴻海'}];
  const before=JSON.stringify(data);
  assert.deepEqual(codes(rank(data).positive),['1101','2317','2330']);
  assert.deepEqual(codes(rank(data,'amount',' 台積 ').positive),['2330']);
  assert.deepEqual(codes(rank(data,'amount','2317').positive),['2317']);
  assert.equal(JSON.stringify(data),before);
});
test('空資料及無結果不產生無窮比例尺或填補名額',()=>{
  assert.equal(context.rankActiveHoldings(null).scale,0);
  assert.equal(rank([stock('2330',1)],'amount','查無股票').matched,0);
  assert.equal(rank([stock('2330',1)]).positive.length,1);
});
test('資料端最小六位小數估值不顯示成零',()=>{
  const formatContext=vm.createContext({
    hasNumber:v=>typeof v==='number'&&Number.isFinite(v),
    signed:(v,u,d)=>v==null?'—':v.toFixed(d)+u
  });
  vm.runInContext(html.match(/  const activeAmount=.*;/)[0]+'\nthis.format=activeAmount;',formatContext);
  assert.equal(formatContext.format(.000001),'0.000001 億元');
  assert.equal(formatContext.format(-.000001),'-0.000001 億元');
  assert.equal(formatContext.format(null),'—');
});
test('資料不足的週月不出榜，不將舊 active_changes 當成新合約備援',()=>{
  const body=html.match(/  function renderActiveHoldings\([\s\S]*?(?=  \$\("active-period"\))/)[0];
  const elements=new Map();
  const element=id=>{
    if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',options:[]});
    return elements.get(id);
  };
  const ctx=vm.createContext({
    $:element,state:{activePeriod:'1w',activeMetric:'amount',activeQuery:''},
    activeHoldings:{version:1,as_of:'2026-10-20'},detailIsDemo:false,detailDate:'2026-10-20',dataFailed:false,
    activePeriodData:()=>({status:'資料不足',reason:'缺少期初資料',stocks:[stock('2330',999)]}),
    activePeriodLabel:()=>'近一週',rankActiveHoldings:context.rankActiveHoldings,
    hasNumber:v=>typeof v==='number'&&Number.isFinite(v),
    detailText:v=>String(v??'—'),esc:v=>String(v),activeETFButton:()=>'',fmt:String,signed:String
  });
  vm.runInContext(body+'\nrenderActiveHoldings();',ctx);
  assert.match(element('active-grid').innerHTML,/缺少期初資料/);
  assert.doesNotMatch(element('active-grid').innerHTML,/2330|999/);
});
