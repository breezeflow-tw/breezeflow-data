// 本機互動預覽：只監聽 127.0.0.1，測試驗證僅存在此程式，不部署至 Cloudflare。
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {resolve,sep,extname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {localDB} from './local-db.mjs';
import {handle} from './worker.mjs';
const port=Number(process.env.PORT||8769),origin='http://127.0.0.1:'+port;
const root=fileURLToPath(new URL('../web/',import.meta.url));
const DB=localDB(fileURLToPath(new URL('./preview.db',import.meta.url)));
const env={DB,ALLOWED_ORIGIN:origin,POSTING_ENABLED:'true',RATE_SECRET:'local-preview-rate-secret-only-not-for-production',ADMIN_TOKEN:'local-preview-admin-only-not-for-production'};
if(!DB.sqlite.prepare('SELECT id FROM posts LIMIT 1').get()){
  const insert=DB.sqlite.prepare('INSERT INTO posts(thread_id,request_id,author,title,category,body,created_at) VALUES(?,?,?,?,?,?,?)');
  insert.run(null,crypto.randomUUID(),'測試訪客甲','資料查核時，怎麼描述日期差異？','資料查核','【本機測試，非真實訪客】想了解市價日期與投信淨值日期不同時，應如何提供勘誤資訊。','2026-10-23T13:00:00Z');
  insert.run(1,crypto.randomUUID(),'測試訪客乙','','資料查核','【本機測試回覆】可以一起附上 ETF 代號、兩個資料日期及官方公告連結，方便站長核對。','2026-10-23T13:10:00Z');
  insert.run(null,crypto.randomUUID(),'測試訪客丙','希望能保留常用的 ETF 篩選條件','網站建議','【本機測試，非真實訪客】想討論使用網站時會用到的功能。這則內容只示範主題列表。','2026-10-23T13:20:00Z');
}
createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,origin);
    if(url.pathname==='/discussion-config.json'){
      res.writeHead(200,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify({enabled:true,apiUrl:origin+'/api',turnstileSitekey:'',preview:true}));return;
    }
    if(url.pathname.startsWith('/api/')){
      const chunks=[];let bytes=0;for await(const chunk of req){bytes+=chunk.length;if(bytes>14000){res.writeHead(413);res.end();return;}chunks.push(chunk);}
      const request=new Request(origin+url.pathname.slice(4)+url.search,{method:req.method,headers:{...req.headers,'CF-Connecting-IP':'127.0.0.1'},...(['GET','HEAD'].includes(req.method)?{}:{body:Buffer.concat(chunks)})});
      const result=await handle(request,env,{verify:async token=>token==='local-preview'});res.writeHead(result.status,Object.fromEntries(result.headers));res.end(await result.text());return;
    }
    const path=resolve(root,'.'+decodeURIComponent(url.pathname==='/'?'/index.html':url.pathname));
    if(!path.startsWith(root.endsWith(sep)?root:root+sep)){res.writeHead(403);res.end();return;}
    const bytes=await readFile(path);const type={'.html':'text/html; charset=utf-8','.json':'application/json','.css':'text/css','.js':'text/javascript'}[extname(path)]||'application/octet-stream';
    res.writeHead(200,{'Content-Type':type,'Cache-Control':'no-store'});res.end(bytes);
  }catch{res.writeHead(404);res.end('Not found');}
}).listen(port,'127.0.0.1',()=>console.log('本機討論板預覽：'+origin+'/#discussion（測試資料）'));
