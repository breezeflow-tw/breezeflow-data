// 網站靜態檔檢查：網頁引用的圖示、設定檔與外掛都存在；外掛有授權全文，雜湊值與 docs/third-party.md 紀錄一致。
import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {existsSync,readFileSync,readdirSync} from 'node:fs';

const web=new URL('../web/',import.meta.url);
const html=readFileSync(new URL('index.html',web),'utf8');
const head=html.slice(0,html.indexOf('</head>'));
const local=ref=>!/^(https?:|data:|#)/.test(ref);

test('網頁開頭引用的本機檔案都存在',()=>{
  const refs=Array.from(head.matchAll(/<(?:link|script)\b[^>]*?\b(?:href|src)="([^"]+)"/g),m=>m[1]).filter(local);
  assert.ok(refs.includes('manifest.webmanifest'));assert.ok(refs.includes('favicon.svg'));
  for(const ref of refs)assert.ok(existsSync(new URL(ref,web)),ref);
});

test('分享預覽圖網址指向網站上的 og.png',()=>{
  const image=head.match(/<meta property="og:image" content="([^"]+)"/)[1];
  assert.match(image,/^https:\/\/breezeflow-tw\.github\.io\/breezeflow-data\/og\.png$/);
  assert.ok(existsSync(new URL('og.png',web)));
});

test('加到主畫面設定可解析，圖示檔都存在',()=>{
  const manifest=JSON.parse(readFileSync(new URL('manifest.webmanifest',web),'utf8'));
  assert.equal(manifest.start_url,'./');assert.equal(manifest.display,'standalone');
  assert.ok(manifest.icons.some(i=>i.purpose==='maskable'));
  for(const icon of manifest.icons)assert.ok(existsSync(new URL(icon.src,web)),icon.src);
});

test('外掛檔名帶版本、有授權全文，雜湊值記在 docs/third-party.md',()=>{
  const record=readFileSync(new URL('../docs/third-party.md',import.meta.url),'utf8');
  const licenses=readdirSync(new URL('vendor/licenses/',web));
  const scripts=readdirSync(new URL('vendor/',web)).filter(f=>f.endsWith('.js'));
  assert.ok(scripts.length>0);
  for(const file of scripts){
    const [,name,version]=file.match(/^([a-z.]+?)-(\d+\.\d+\.\d+)\b/)||[];
    assert.ok(name&&version,`${file} 檔名要帶版本號`);
    assert.ok(licenses.some(l=>l.startsWith(`${name}`)&&l.includes(version)),`${file} 缺授權全文`);
    const hash=createHash('sha256').update(readFileSync(new URL(`vendor/${file}`,web))).digest('hex');
    assert.ok(record.includes(hash),`${file} 的 SHA-256 未記在 docs/third-party.md`);
    assert.ok(html.includes(`src="vendor/${file}" defer`),`${file} 要以 defer 載入`);
  }
});
