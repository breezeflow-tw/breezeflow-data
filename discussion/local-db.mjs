// 僅供離線測試與本機預覽，正式 Worker 不匯入此檔。
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
export function localDB(path=':memory:'){
  const sqlite=new DatabaseSync(path);
  sqlite.exec('PRAGMA foreign_keys=ON');
  sqlite.exec(readFileSync(new URL('./schema.sql',import.meta.url),'utf8'));
  return {sqlite,close:()=>sqlite.close(),prepare(sql){
    const make=(args=[])=>({
      bind:(...values)=>make(values),
      async all(){return {results:sqlite.prepare(sql).all(...args)};},
      async first(){return sqlite.prepare(sql).get(...args)??null;},
      async run(){const r=sqlite.prepare(sql).run(...args);return {meta:{changes:Number(r.changes),last_row_id:Number(r.lastInsertRowid)}};}
    });return make();
  }};
}
