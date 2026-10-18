-- ETF 基本資料：每檔一列，每天更新
CREATE TABLE IF NOT EXISTS etf (
  code        TEXT PRIMARY KEY,   -- 代號，例如 0050、00679B
  name        TEXT NOT NULL,
  issuer      TEXT,               -- 發行投信
  category    TEXT,               -- 市值型 / 高股息 / 主題型 / 海外股票 / 債券 / 商品 / 槓桿反向 / 主動式
  market      TEXT,               -- 上市 / 上櫃
  fee         REAL,               -- 總費用率（%）
  aum         REAL,               -- 規模（億元）＝ 發行單位數 × 淨值
  units       REAL,               -- 發行單位數
  index_name  TEXT,               -- 追蹤指數
  listed_date TEXT,               -- 上市日期
  mgmt_fee    REAL,               -- 經理費率（%），只有單一費率或投信公布實際費率時才有
  mgmt_fee_text      TEXT,        -- 經理費原文（多為依規模分段）
  custodian_fee_text TEXT,        -- 保管費原文
  manager     TEXT,               -- 基金經理人
  risk_class  TEXT,               -- 風險等級 RR1～RR5
  beneficiaries       INTEGER,    -- 受益人數
  beneficiaries_month TEXT,       -- 受益人數資料年月
  stats_json  TEXT,               -- 投資組合統計（現金殖利率、本益比等），JSON
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
  nav_official     REAL,          -- 投信公告的正式淨值（隔天才會有）
  premium_official REAL,          -- 收盤價對正式淨值的折溢價（%）
  aum          REAL,              -- 當天規模（億元）
  price_check  TEXT,              -- 市價查核結果：一致 / 已修正 / 未查核 / 交易所
  nav_source   TEXT,              -- 淨值出處
  aum_source   TEXT,              -- 規模出處
  beneficiaries INTEGER,          -- 受益人數
  PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code, date);

-- 配息紀錄：從除權除息預告表每天累積
CREATE TABLE IF NOT EXISTS dividend (
  code        TEXT NOT NULL,
  ex_date     TEXT NOT NULL,      -- 除息交易日
  cash        REAL,               -- 每單位現金配息（元），尚未公告時為空
  pay_date    TEXT,               -- 發放日（待接公開資訊觀測站）
  updated_at  TEXT,
  PRIMARY KEY (code, ex_date)
);

-- 市價查核紀錄：Yahoo 股市與交易所數字不一致的明細
CREATE TABLE IF NOT EXISTS price_check (
  date        TEXT NOT NULL,
  code        TEXT NOT NULL,
  field       TEXT NOT NULL,      -- close / volume_lots
  yahoo       REAL,
  exchange    REAL,               -- 網站採用此值
  checked_at  TEXT,
  PRIMARY KEY (date, code, field)
);

-- 成分股：投信公告的申購買回清單，每檔只保留最新一份
CREATE TABLE IF NOT EXISTS holding (
  code        TEXT NOT NULL,      -- ETF 代號
  date        TEXT,               -- 清單日期
  asset_code  TEXT NOT NULL,      -- 成分代號（期貨加上月份，例如 TX-202610）
  asset_name  TEXT,
  kind        TEXT NOT NULL,      -- 股票 / 債券 / 基金 / 期貨
  weight      REAL,               -- 權重（%）
  qty         REAL,               -- 股數、口數或面額
  source      TEXT,
  PRIMARY KEY (code, asset_code, kind)
);
