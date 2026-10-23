// Cloudflare Workers 原生 API；不使用 AI、外部套件或前端管理密鑰。
const CATEGORIES=['資料查核','ETF 討論','網站建議','其他'];
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i;
const PUBLIC_FIELDS='id,thread_id,author,title,category,body,created_at,locked';
class APIError extends Error { constructor(status,message){super(message);this.status=status;} }
const fail=(status,message)=>{throw new APIError(status,message);};
function text(value,min,max,label){
  if(typeof value!=='string')fail(400,`${label}格式不正確。`);
  const result=value.normalize('NFC').trim();
  if(result.length<min||result.length>max||/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(result))fail(400,`${label}需為 ${min}–${max} 字。`);
  return result;
}
export function validatePost(data){
  if(!data||typeof data!=='object'||Array.isArray(data))fail(400,'留言格式不正確。');
  if(data.consent!==true)fail(400,'請先同意公開留言規則。');
  if(data.website)fail(400,'無法送出此留言。');
  if(!UUID.test(data.request_id||''))fail(400,'送出識別碼不正確，請重新整理後再試。');
  const author=text(data.author,1,24,'暱稱');
  if(/站長|管理員|官方|admin|moderator|breezeflow|微風流動/i.test(author))fail(400,'請勿以站長、官方或管理員身分發言。');
  const thread_id=data.thread_id??null;
  if(thread_id!==null&&(!Number.isSafeInteger(thread_id)||thread_id<1))fail(400,'討論主題不存在。');
  const title=thread_id===null?text(data.title,4,80,'主題'):'';
  const category=thread_id===null?data.category:'其他';
  if(!CATEGORIES.includes(category))fail(400,'請選擇有效的討論分類。');
  return {request_id:data.request_id,author,title,category,thread_id,body:text(data.body,4,2000,'內容')};
}
function integer(value,fallback){
  if(value===null)return fallback;
  if(!/^\d+$/.test(value)||!Number.isSafeInteger(Number(value))||Number(value)<0)fail(400,'分頁參數不正確。');
  return Number(value);
}
async function jsonBody(request){
  if(!request.headers.get('content-type')?.startsWith('application/json'))fail(415,'僅接受 JSON 格式。');
  if(Number(request.headers.get('content-length'))>14000)fail(413,'留言過長。');
  const reader=request.body?.getReader();if(!reader)fail(400,'請輸入留言。');
  let length=0,chunks=[];
  while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>14000){await reader.cancel();fail(413,'留言過長。');}chunks.push(value);}
  const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
  try{return JSON.parse(new TextDecoder().decode(bytes));}catch{fail(400,'留言格式不正確。');}
}
async function digest(value){return new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value)));}
async function authenticate(request,env){
  if(typeof env.ADMIN_TOKEN!=='string'||env.ADMIN_TOKEN.length<32)fail(503,'管理功能尚未設定。');
  const supplied=request.headers.get('authorization')||'';
  const a=await digest(supplied),b=await digest('Bearer '+env.ADMIN_TOKEN);
  let difference=0;for(let i=0;i<a.length;i++)difference|=a[i]^b[i];
  if(difference)fail(401,'管理密鑰不正確。');
}
async function clientKey(request,env,day){
  const ip=request.headers.get('CF-Connecting-IP');
  if(!ip||typeof env.RATE_SECRET!=='string'||env.RATE_SECRET.length<32)fail(503,'留言服務尚未完成安全設定。');
  const key=await crypto.subtle.importKey('raw',new TextEncoder().encode(env.RATE_SECRET),{name:'HMAC',hash:'SHA-256'},false,['sign']);
  const signed=new Uint8Array(await crypto.subtle.sign('HMAC',key,new TextEncoder().encode(day+'|'+ip)));
  return Array.from(signed,b=>b.toString(16).padStart(2,'0')).join('');
}
async function limit(db,key,max,expires){
  // 條件更新與計數在同一個 SQL 中完成，並行請求也不會超過設定次數。
  const result=await db.prepare('INSERT INTO rate_limits(key,count,expires_at) VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1 WHERE count<? RETURNING count').bind(key,expires,max).all();
  if(!result.results.length)fail(429,'送出過於頻繁，請稍後再試。');
}
async function verifyTurnstile(token,request,env){
  if(typeof env.TURNSTILE_SECRET!=='string'||!env.TURNSTILE_SECRET)fail(503,'驗證服務尚未設定。');
  if(typeof token!=='string'||!token||token.length>2048)fail(400,'請完成防機器人驗證。');
  const response=await fetch('https://challenges.cloudflare.com/turnstile/v0/siteverify',{
    method:'POST',headers:{'Content-Type':'application/json'},signal:AbortSignal.timeout(10000),
    body:JSON.stringify({secret:env.TURNSTILE_SECRET,response:token,remoteip:request.headers.get('CF-Connecting-IP')})
  });
  if(!response.ok)fail(503,'驗證服務暫時無法使用，請稍後再試。');
  const result=await response.json();
  return result.success===true&&result.hostname===new URL(env.ALLOWED_ORIGIN).hostname&&result.action==='discussion';
}
export async function handle(request,env,{verify=verifyTurnstile,now=()=>Date.now()}={}){
  const origin=request.headers.get('origin');
  const allowed=env.ALLOWED_ORIGIN;
  const headers={'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Vary':'Origin'};
  if(origin===allowed)headers['Access-Control-Allow-Origin']=allowed;
  const respond=(data,status=200)=>new Response(JSON.stringify(data),{status,headers});
  try{
    if(!allowed||!env.DB)fail(503,'討論板尚未啟用。');
    if(origin&&origin!==allowed)fail(403,'此網站無法使用討論板服務。');
    if(request.method==='OPTIONS')return new Response(null,{status:204,headers:{...headers,'Access-Control-Allow-Methods':'GET, POST, OPTIONS','Access-Control-Allow-Headers':'Content-Type, Authorization','Access-Control-Max-Age':'600'}});
    const url=new URL(request.url),path=url.pathname;
    if(path.startsWith('/admin/')){
      await authenticate(request,env);
      if(path==='/admin/posts'&&request.method==='GET'){
        const before=integer(url.searchParams.get('before'),Number.MAX_SAFE_INTEGER);
        const rows=await env.DB.prepare(`SELECT ${PUBLIC_FIELDS},hidden FROM posts WHERE id<? ORDER BY id DESC LIMIT 51`).bind(before).all();
        return respond({items:rows.results.slice(0,50),next:rows.results.length>50?rows.results[49].id:null});
      }
      if(path==='/admin/moderate'&&request.method==='POST'){
        const data=await jsonBody(request);
        if(!Number.isSafeInteger(data.id)||data.id<1)fail(400,'留言編號不正確。');
        const operations={hide:['hidden',1],restore:['hidden',0],lock:['locked',1],unlock:['locked',0]};
        if(!Object.hasOwn(operations,data.operation))fail(400,'管理操作不正確。');
        const [field,value]=operations[data.operation];
        const changed=await env.DB.prepare(`UPDATE posts SET ${field}=? WHERE id=?`).bind(value,data.id).run();
        if(!changed.meta.changes)fail(404,'找不到這則留言。');
        return respond({ok:true});
      }
      fail(404,'找不到此功能。');
    }
    if(path==='/threads'&&request.method==='GET'){
      const before=integer(url.searchParams.get('before'),Number.MAX_SAFE_INTEGER);
      const rows=await env.DB.prepare(`SELECT ${PUBLIC_FIELDS},(SELECT count(*) FROM posts r WHERE r.thread_id=posts.id AND r.hidden=0) AS replies FROM posts WHERE thread_id IS NULL AND hidden=0 AND id<? ORDER BY id DESC LIMIT 21`).bind(before).all();
      return respond({items:rows.results.slice(0,20),next:rows.results.length>20?rows.results[19].id:null});
    }
    const thread=path.match(/^\/threads\/([1-9]\d*)$/);
    if(thread&&request.method==='GET'){
      const id=integer(thread[1],0),after=integer(url.searchParams.get('after'),0);
      const root=await env.DB.prepare(`SELECT ${PUBLIC_FIELDS} FROM posts WHERE id=? AND thread_id IS NULL AND hidden=0`).bind(id).first();
      if(!root)fail(404,'這個主題不存在或已由站長隱藏。');
      const rows=await env.DB.prepare(`SELECT ${PUBLIC_FIELDS} FROM posts WHERE thread_id=? AND hidden=0 AND id>? ORDER BY id LIMIT 51`).bind(id,after).all();
      return respond({thread:root,items:rows.results.slice(0,50),next:rows.results.length>50?rows.results[49].id:null});
    }
    if(path==='/posts'&&request.method==='POST'){
      if(env.POSTING_ENABLED!=='true')fail(503,'討論板目前暫停發文，仍可閱讀已公開內容。');
      const data=await jsonBody(request),post=validatePost(data);
      const existing=await env.DB.prepare('SELECT id,thread_id,hidden,author,title,category,body FROM posts WHERE request_id=?').bind(post.request_id).first();
      if(existing){
        if(existing.hidden)fail(409,'這次送出的留言已由站長隱藏。');
        if(['author','title','body','thread_id'].some(key=>existing[key]!==post[key])||(post.thread_id===null&&existing.category!==post.category))fail(409,'前次留言已送出；請先更新討論確認，再發起新留言。');
        return respond({id:existing.id,thread_id:existing.thread_id??existing.id});
      }
      const time=now(),date=new Date(time).toISOString(),day=date.slice(0,10),key=await clientKey(request,env,day);
      await env.DB.prepare('DELETE FROM rate_limits WHERE expires_at<?').bind(time).run();
      await limit(env.DB,'minute:'+Math.floor(time/60000)+':'+key,6,time+172800000);
      await limit(env.DB,'day:'+day+':'+key,40,time+172800000);
      if(!await verify(data.token,request,env))fail(400,'驗證失敗或已過期，請重新驗證。');
      await limit(env.DB,'site:'+day,1000,time+172800000);
      let result;
      if(post.thread_id!==null){
        // 寫入當下再次檢查主題狀態，避免隱藏／鎖定後仍收到回覆。
        result=await env.DB.prepare('INSERT INTO posts(thread_id,request_id,author,title,category,body,created_at) SELECT id,?,?,?,category,?,? FROM posts WHERE id=? AND thread_id IS NULL AND hidden=0 AND locked=0').bind(post.request_id,post.author,'',post.body,date,post.thread_id).run();
        if(!result.meta.changes)fail(409,'主題已關閉或隱藏，無法新增回覆。');
      }else{
        result=await env.DB.prepare('INSERT INTO posts(request_id,author,title,category,body,created_at) VALUES(?,?,?,?,?,?)').bind(post.request_id,post.author,post.title,post.category,post.body,date).run();
      }
      return respond({id:result.meta.last_row_id,thread_id:post.thread_id??result.meta.last_row_id},201);
    }
    fail(404,'找不到此功能。');
  }catch(error){
    // 不把 SQL、密鑰、連線資料或服務內部錯誤傳回訪客。
    return respond({error:error instanceof APIError?error.message:'討論板暫時無法使用，請稍後再試。'},error instanceof APIError?error.status:503);
  }
}
export default {fetch:(request,env)=>handle(request,env)};
