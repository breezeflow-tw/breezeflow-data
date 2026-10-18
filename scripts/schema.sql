-- ETF 基本資料：每檔一列，每天更新
CREATE TABLE IF NOT EXISTS etf (
  code        TEXT PRIMARY KEY,   -- 代號，例如 0050、00679B
  name        TEXT NOT NULL,
  issuer      TEXT,               -- 發行投信
  category    TEXT,               -- 市值型 / 高股息 / 主題型 / 海外股票 / 債券 / 商品 / 槓桿反向 / 主動式
  market      TEXT,               -- 上市 / 上櫃
  fee         REAL,               -- 總費用率（%）
  aum         REAL,               -- 規模（億元）
  updated_at  TEXT
);

-- 每日快照：每檔每天一列，持續累積成歷史資料
CREATE TABLE IF NOT EXISTS daily (
  date         TEXT NOT NULL,     -- 交易日 YYYY-MM-DD
  code         TEXT NOT NULL REFERENCES etf(code),
  price        REAL,              -- 收盤價（無收盤價時用即時成交價）
  change_pct   REAL,              -- 漲跌幅（%）
  nav          REAL,              -- 投信預估淨值
  nav_prev     REAL,              -- 前一營業日淨值
  premium      REAL,              -- 折溢價（%），正數為溢價
  volume_lots  INTEGER,           -- 成交量（張）
  source_time  TEXT,              -- 淨值資料時間
  PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code, date);
