import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8'),c=vm.createContext({URL,Intl});
vm.runInContext(html.slice(html.indexOf('  function forumEscape('),html.indexOf('  // 討論板顯示核心結束。')),c);
test('公開討論中的 HTML、事件與暱稱都只呈現為文字',()=>{
  const value=c.forumPost({id:1,author:'<img src=x onerror=alert(1)>',title:'<script>test</script>',body:'<iframe src="https://x">文字 & 內容</iframe>',created_at:'2026-10-23T13:00:00Z'},{heading:true});
  assert.doesNotMatch(value,/<script|<img|<iframe|<a href="https:\/\/x/);assert.match(value,/&lt;iframe/);assert.match(value,/訪客/);assert.match(value,/#1/);
});
test('正式網站不能使用本機測試模式或不安全的後端',()=>{
  const live='https://breezeflow-tw.github.io/breezeflow-data/';
  assert.throws(()=>c.forumConfig({enabled:true,apiUrl:'http://127.0.0.1:8769/api',preview:true},live));
  assert.throws(()=>c.forumConfig({enabled:true,apiUrl:'https://secret@example.com',turnstileSitekey:'public'},live));
  assert.equal(c.forumConfig({enabled:true,apiUrl:'https://test.workers.dev',turnstileSitekey:'public'},live).preview,false);
  assert.equal(c.forumConfig({enabled:true,apiUrl:'http://127.0.0.1:8769/api',preview:true},'http://127.0.0.1:8769/').preview,true);
});
test('未設定的服務會明示未啟用，不產生假的成功狀態',()=>{
  assert.throws(()=>c.forumConfig({enabled:false},'https://example.com'),/尚未啟用/);
  assert.throws(()=>c.forumConfig(null,'https://example.com'),/尚未啟用/);
});
test('日期明確使用臺北時間，未知日期不補猜',()=>{
  assert.match(c.forumDate('2026-10-23T13:00:00Z'),/21:00/);
  for(const value of ['bad','',null,undefined])assert.equal(c.forumDate(value),'日期未提供');
});
