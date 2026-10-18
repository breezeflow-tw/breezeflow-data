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
  fee_source  TEXT,               -- 經理費、保管費原文的出處（投信官網或投信投顧公會）
  expense_json TEXT,              -- 投信投顧公會公布的年度實際費用比率，JSON
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
  units_change REAL,              -- 當日申購買回造成的發行單位數增減（正數為淨申購，可看資金流向）
  PRIMARY KEY (date, code)
);

CREATE INDEX IF NOT EXISTS idx_daily_code ON daily(code, date);

-- 配息紀錄：投信官網配息紀錄為主，交易所除權息預告補充，每天累積
CREATE TABLE IF NOT EXISTS dividend (
  code        TEXT NOT NULL,
  ex_date     TEXT NOT NULL,      -- 除息交易日
  cash        REAL,               -- 每單位現金配息（元），尚未公告時為空
  pay_date    TEXT,               -- 配息發放日（投信官網提供）
  source      TEXT,               -- 出處：投信官網 / 交易所除權息預告
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

-- 成分股：投信公告的申購買回清單，依清單日期保存歷史
CREATE TABLE IF NOT EXISTS holding (
  code        TEXT NOT NULL,      -- ETF 代號
  date        TEXT,               -- 清單日期
  asset_code  TEXT NOT NULL,      -- 成分代號（期貨加上月份，例如 TX-202610）
  asset_name  TEXT,
  kind        TEXT NOT NULL,      -- 股票 / 債券 / 基金 / 期貨
  weight      REAL,               -- 權重（%）
  qty         REAL,               -- 股數、口數或面額
  source      TEXT,
  PRIMARY KEY (code, date, asset_code, kind)
);

-- 投信投顧公會的基金資料：契約費率、經理人、風險等級（每月更新），以及年度實際費用比率（%）
CREATE TABLE IF NOT EXISTS fund_info (
  code          TEXT PRIMARY KEY,   -- ETF 代號（公會的受益憑證代號）
  fund_id       TEXT,               -- 基金統編
  name          TEXT,               -- 基金全名
  data_month    TEXT,               -- 基本資料年月 YYYY-MM
  inception     TEXT,               -- 基金成立日
  currency      TEXT,
  manager       TEXT,
  risk_class    TEXT,
  mgmt_fee_text TEXT,               -- 契約經理費率原文
  custodian_fee_text TEXT,          -- 契約保管費率原文
  expense_year  INTEGER,            -- 實際費用比率的年度（年中才成立的基金不收）
  exp_mgmt      REAL,
  exp_custodian REAL,
  exp_other     REAL,
  exp_total     REAL,               -- 合計（含交易成本）
  fetched       TEXT                -- 抓取日期
);

-- 投信投顧公會基金資料彙總表（月）：月底規模、受益人數與當月申購買回金額，依月份累積
CREATE TABLE IF NOT EXISTS fund_month (
  code          TEXT NOT NULL,      -- ETF 代號
  month         TEXT NOT NULL,      -- 資料年月 YYYY-MM
  aum_twd       REAL,               -- 月底基金規模（新台幣元）
  nav           REAL,               -- 月底單位淨值（新台幣）
  units         REAL,               -- 受益權單位數
  beneficiaries INTEGER,            -- 總受益人數
  beneficiaries_natural INTEGER,    -- 自然人受益人數
  beneficiaries_legal   INTEGER,    -- 法人受益人數
  subscribe_twd REAL,               -- 當月申購總金額（新台幣元）
  redeem_twd    REAL,               -- 當月買回總金額（新台幣元）
  PRIMARY KEY (code, month)
);

-- 投信投顧公會基金投資明細（月前十大）：每檔 ETF 月底前十大投資標的，依月份累積
CREATE TABLE IF NOT EXISTS fund_top (
  code        TEXT NOT NULL,      -- ETF 代號
  month       TEXT NOT NULL,      -- 資料年月 YYYY-MM
  rank        INTEGER NOT NULL,   -- 名次 1～10
  asset_code  TEXT,               -- 標的代號（國內股票為證券代號，國外標的多為 ISIN）
  asset_name  TEXT,
  kind        TEXT,               -- 股票 / 債券 / 基金 / 期貨 / 其他
  weight      REAL,               -- 占基金淨資產價值比例（%）
  PRIMARY KEY (code, month, rank)
);

-- 投信投顧公會歷史資料的回補進度：哪些報表的哪些月份已經抓過
CREATE TABLE IF NOT EXISTS fund_fetch (
  report   TEXT NOT NULL,         -- summary（基金資料彙總表）/ dividend（基金配息資料）
  month    TEXT NOT NULL,         -- 資料年月 YYYY-MM
  fetched  TEXT,                  -- 抓取日期
  PRIMARY KEY (report, month)
);
