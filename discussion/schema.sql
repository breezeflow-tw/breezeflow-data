-- 留言獨立存於 Cloudflare D1，不混入 ETF 資料庫或公開 Git 歷史。
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id INTEGER REFERENCES posts(id),
  request_id TEXT NOT NULL UNIQUE,
  author TEXT NOT NULL CHECK(length(author) BETWEEN 1 AND 24),
  title TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL CHECK(category IN ('資料查核','ETF 討論','網站建議','其他')),
  body TEXT NOT NULL CHECK(length(body) BETWEEN 1 AND 2000),
  created_at TEXT NOT NULL,
  hidden INTEGER NOT NULL DEFAULT 0 CHECK(hidden IN (0,1)),
  locked INTEGER NOT NULL DEFAULT 0 CHECK(locked IN (0,1))
);
CREATE INDEX IF NOT EXISTS posts_threads ON posts(hidden, thread_id, id DESC);
CREATE INDEX IF NOT EXISTS posts_replies ON posts(thread_id, hidden, id);
CREATE TABLE IF NOT EXISTS rate_limits (
  key TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_expiry ON rate_limits(expires_at);
