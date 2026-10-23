import test from 'node:test';
import assert from 'node:assert/strict';
import worker,{handle,validatePost} from '../discussion/worker.mjs';
import {localDB} from '../discussion/local-db.mjs';
const origin='https://breezeflow-tw.github.io';
const authorToken='a'.repeat(48);
function setup(t){const DB=localDB();t.after(()=>DB.close());return {DB,ALLOWED_ORIGIN:origin,POSTING_ENABLED:'true',RATE_SECRET:'r'.repeat(48),ADMIN_TOKEN:authorToken};}
function post(extra={}){return {request_id:crypto.randomUUID(),author:'測試訪客',title:'測試資料查核問題',category:'資料查核',body:'這是一則離線測試留言。',consent:true,token:'測試驗證',...extra};}
async function request(env,path,body,options={}){
  const {method=body?'POST':'GET',admin=false,verify=async()=>true,ip='192.0.2.1',headers={}}=options;
  return handle(new Request('https://test.workers.dev'+path,{method,headers:{Origin:origin,'Content-Type':'application/json','CF-Connecting-IP':ip,...(admin?{Authorization:'Bearer '+authorToken}:{}),...headers},...(body?{body:JSON.stringify(body)}:{})}),env,{verify,now:()=>Date.parse('2026-10-23T12:00:00Z')});
}
test('免註冊發文立即可讀，回覆繫結同一主題且不暴露請求識別碼',async t=>{
  const env=setup(t),p=await request(env,'/posts',post()),id=(await p.json()).id;assert.equal(p.status,201);
  const reply=await request(env,'/posts',post({thread_id:id,title:'',category:''}));assert.equal(reply.status,201);
  const detail=await (await request(env,'/threads/'+id)).json();assert.equal(detail.items.length,1);assert.equal(detail.thread.author,'測試訪客');assert.equal(detail.items[0].category,'資料查核');assert.equal(detail.thread.request_id,undefined);
  const list=await (await request(env,'/threads')).json();assert.equal(list.items[0].replies,1);
});
test('重送相同請求不會重複發文',async t=>{
  const env=setup(t),body=post();const one=await (await request(env,'/posts',body)).json();
  const two=await (await request(env,'/posts',body,{verify:()=>{throw Error('重送不重用驗證');}})).json();
  assert.equal(one.id,two.id);assert.equal((await (await request(env,'/threads')).json()).items.length,1);
  assert.equal((await request(env,'/posts',{...body,body:'修改過的內容不能冒充原本的重送'})).status,409);
});
test('未通過驗證、缺同意或冒用管理員名稱都不能發文',async t=>{
  const env=setup(t);assert.equal((await request(env,'/posts',post(),{verify:async()=>false})).status,400);
  for(const change of [{consent:false},{author:'官方站長'},{body:'x'.repeat(2001)},{thread_id:-1},{category:'陌生分類'},{request_id:'bad'},{website:'垃圾'}])assert.equal((await request(env,'/posts',post(change))).status,400);
  assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM posts').get().n,0);
});
test('同一連線每分鐘上限會在資料庫執行，拒絕第七次發文',async t=>{
  const env=setup(t);for(let i=0;i<6;i++)assert.equal((await request(env,'/posts',post())).status,201);
  assert.equal((await request(env,'/posts',post())).status,429);
  const keys=env.DB.sqlite.prepare('SELECT key FROM rate_limits').all();assert.ok(keys.every(x=>!x.key.includes('192.0.2.1')));
});
test('跨來源請求被拒，管理權限不能靠前端隱藏',async t=>{
  const env=setup(t);const denied=await request(env,'/posts',post(),{headers:{Origin:'https://other.example'}});assert.equal(denied.status,403);assert.equal(denied.headers.get('Access-Control-Allow-Origin'),null);
  assert.equal((await request(env,'/admin/posts')).status,401);assert.equal((await request(env,'/admin/moderate',{id:1,operation:'hide'})).status,401);
  const valid=await request(env,'/admin/posts',null,{admin:true});assert.equal(valid.status,200);assert.equal(valid.headers.get('Access-Control-Allow-Origin'),origin);
});
test('隱藏主題同時停止公開回覆，恢復後仍保有原留言',async t=>{
  const env=setup(t),id=(await (await request(env,'/posts',post())).json()).id;
  await request(env,'/posts',post({thread_id:id}));await request(env,'/admin/moderate',{id,operation:'hide'},{admin:true});
  assert.equal((await request(env,'/threads/'+id)).status,404);assert.equal((await request(env,'/posts',post({thread_id:id}))).status,409);
  assert.equal((await (await request(env,'/threads')).json()).items.length,0);
  assert.equal((await (await request(env,'/admin/posts',null,{admin:true})).json()).items.length,2);
  await request(env,'/admin/moderate',{id,operation:'restore'},{admin:true});assert.equal((await (await request(env,'/threads/'+id)).json()).items.length,1);
});
test('鎖定只停止新回覆，既有內容仍可閱讀',async t=>{
  const env=setup(t),id=(await (await request(env,'/posts',post())).json()).id;
  await request(env,'/admin/moderate',{id,operation:'lock'},{admin:true});assert.equal((await request(env,'/posts',post({thread_id:id}))).status,409);assert.equal((await request(env,'/threads/'+id)).status,200);
  await request(env,'/admin/moderate',{id,operation:'unlock'},{admin:true});assert.equal((await request(env,'/posts',post({thread_id:id}))).status,201);
});
test('管理隱藏單則回覆不影響其他內容，讀取不洩漏隱藏文字',async t=>{
  const env=setup(t),id=(await (await request(env,'/posts',post())).json()).id;
  const reply=(await (await request(env,'/posts',post({thread_id:id,body:'待隱藏的測試內容'}))).json()).id;
  await request(env,'/admin/moderate',{id:reply,operation:'hide'},{admin:true});const response=await request(env,'/threads/'+id);assert.doesNotMatch(await response.text(),/待隱藏/);
});
test('資料庫查詢使用參數，HTML 只儲存為文字',async t=>{
  const env=setup(t),body="<script>alert('x')</script> '); DROP TABLE posts; --";
  const id=(await (await request(env,'/posts',post({body}))).json()).id;const detail=await (await request(env,'/threads/'+id)).json();assert.equal(detail.thread.body,body);
  assert.equal((await request(env,'/threads?before=1%20OR%201=1')).status,400);
});
test('分頁有界，重複讀取與非法參數不會全表輸出',async t=>{
  const env=setup(t);for(let i=0;i<23;i++)await request(env,'/posts',post(),{ip:'192.0.2.'+(i+1)});
  const page=await (await request(env,'/threads')).json();assert.equal(page.items.length,20);const next=await (await request(env,'/threads?before='+page.next)).json();assert.equal(next.items.length,3);assert.equal(next.next,null);
});
test('服務停用與密鑰缺漏會停止發文，仍保留讀取',async t=>{
  const env=setup(t);env.POSTING_ENABLED='false';assert.equal((await request(env,'/posts',post())).status,503);assert.equal((await request(env,'/threads')).status,200);
  env.POSTING_ENABLED='true';env.RATE_SECRET='';assert.equal((await request(env,'/posts',post())).status,503);
});
test('過長本文、錯誤媒體類型與內部錯誤不洩漏資訊',async t=>{
  const env=setup(t);assert.equal((await request(env,'/posts',post({body:'x'.repeat(15000)}))).status,413);
  assert.equal((await request(env,'/posts',post(),{headers:{'Content-Type':'text/plain'}})).status,415);
  env.DB={prepare(){throw Error('secret SQL details');}};const failed=await request(env,'/threads');assert.equal(failed.status,503);assert.doesNotMatch(await failed.text(),/secret SQL/);
});

test('正式入口強制驗證成功、本站網域與討論用途；本機測試字串不能繞過',async t=>{
  const env=setup(t);env.TURNSTILE_SECRET='離線測試密鑰';
  let result={success:true,hostname:'別的網站.example',action:'discussion'};
  const mock=t.mock.method(globalThis,'fetch',async(url,options)=>{
    assert.equal(url,'https://challenges.cloudflare.com/turnstile/v0/siteverify');
    assert.equal(JSON.parse(options.body).secret,env.TURNSTILE_SECRET);
    return Response.json(result);
  });
  const send=token=>worker.fetch(new Request('https://test.workers.dev/posts',{method:'POST',headers:{Origin:origin,'Content-Type':'application/json','CF-Connecting-IP':'192.0.2.1'},body:JSON.stringify(post({token}))}),env);
  assert.equal((await send('local-preview')).status,400);
  result={success:true,hostname:'breezeflow-tw.github.io',action:'other'};assert.equal((await send('test-token')).status,400);
  result={success:false,hostname:'breezeflow-tw.github.io',action:'discussion'};assert.equal((await send('test-token')).status,400);
  result={success:true,hostname:'breezeflow-tw.github.io',action:'discussion'};assert.equal((await send('test-token')).status,201);
  assert.equal(mock.mock.callCount(),4);assert.equal(env.DB.sqlite.prepare('SELECT count(*) n FROM posts').get().n,1);
});
