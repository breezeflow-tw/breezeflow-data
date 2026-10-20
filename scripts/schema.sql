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
  source_url  TEXT,
  PRIMARY KEY (code, date, asset_code, kind)
);

-- 投信原公告快照，與交易所預估資料分開保存，避免混用日期及來源計算資金流。
-- 官方基金日快照：date 是資料日期；announced 是投信標示的公告日期（申購買回清單的清單日期，沒有標示時為空）；
-- first_observed_at／observed_at 是第一次與最近一次取得的時間
CREATE TABLE IF NOT EXISTS fund_daily (
  code TEXT NOT NULL, date TEXT NOT NULL, nav REAL, aum REAL,
  units REAL, units_change REAL, currency TEXT, source TEXT NOT NULL,
  source_url TEXT NOT NULL, observed_at TEXT, announced TEXT, first_observed_at TEXT, PRIMARY KEY(code,date)
);
-- 交易所 OpenAPI 的 ETF 收盤行情原值（上市 STOCK_DAY_ALL、上櫃 tpex_mainboard_daily_close_quotes），逐筆記錄來源與取得時間；
-- 兩個端點都只有最新一天，本站逐日累積。價格是空值代表沒有成交
CREATE TABLE IF NOT EXISTS exchange_daily (
  code TEXT NOT NULL, date TEXT NOT NULL,       -- 交易所標示的資料日期
  market TEXT, name TEXT,
  open REAL, high REAL, low REAL, close REAL, change REAL,   -- 交易所原值（元）
  volume_shares REAL, trade_value REAL, transactions REAL,   -- 成交股數、成交金額（元）、成交筆數
  dataset TEXT NOT NULL, source_url TEXT NOT NULL,           -- 資料集名稱與取得的端點
  first_observed_at TEXT, observed_at TEXT, PRIMARY KEY(code, date)
);
-- 投信官網完整持股清單（申購買回清單）的清單層級證據：出處、列數、台股列數、缺股數列數、權重合計與取得時間。
-- 只記投信官網公告的完整清單；投信投顧公會的月前十大不是完整清單，不記在這裡
CREATE TABLE IF NOT EXISTS holding_list (
  code TEXT NOT NULL, date TEXT NOT NULL,        -- ETF 代號、清單的資料日期
  source TEXT NOT NULL, source_url TEXT,
  rows INTEGER, stock_rows INTEGER, missing_qty INTEGER,   -- 全部列數、台股股票列數、其中缺股數的列數
  weight_sum REAL,                               -- 權重合計（%），現金、附買回等沒列在清單裡時會低於 100
  complete INTEGER NOT NULL,                     -- 1＝投信官網公告的完整清單
  date_basis TEXT,                               -- 已核對的持股日期欄位；空值代表暫不排行
  first_observed_at TEXT, observed_at TEXT, PRIMARY KEY(code, date)
);
-- 主動式 ETF 持有的台股股票收盤價（資料集 11549 上市、11370 上櫃的原值），每次執行依交易所標示的資料日期增量保存；不回補
CREATE TABLE IF NOT EXISTS stock_close (
  code TEXT NOT NULL, date TEXT NOT NULL,
  name TEXT,                                    -- 交易所名稱，核對持股的市場身分
  market TEXT, close REAL,                       -- 收盤價（元）；沒有成交為空值
  dataset TEXT NOT NULL,                         -- 政府資料開放平臺資料集代號
  first_observed_at TEXT, observed_at TEXT, PRIMARY KEY(code, date)
);
-- 台股除權預告（證交所 TWT48U_ALL、櫃買中心 tpex_exright_prepost 標示「權」的列）：預告表只列即將除權的股票，
-- 每次執行保存下來，供持股資金動向排除配股入帳造成的股數變動
CREATE TABLE IF NOT EXISTS stock_event (
  code TEXT NOT NULL, date TEXT NOT NULL,        -- 除權交易日
  kind TEXT NOT NULL,                            -- 官方原文：權、權息、除權、除權息
  stock_dividend_ratio REAL,                     -- 無償配股率（每股配發股數）
  subscription_ratio REAL,                       -- 現金增資認購率
  source TEXT NOT NULL, source_url TEXT NOT NULL, first_observed_at TEXT, observed_at TEXT,
  PRIMARY KEY(code, date, kind)
);
-- 投信官網歷史申購買回清單的回補紀錄：查過的清單日期不再重查
CREATE TABLE IF NOT EXISTS fund_backfill (
  code TEXT NOT NULL, pcf_date TEXT NOT NULL,   -- 查詢的清單日期
  data_date TEXT,                                -- 清單裡的資料日期（淨值日期）；查無資料時為空
  status TEXT NOT NULL,                          -- ok／empty（查無資料）
  source TEXT NOT NULL, fetched_at TEXT, PRIMARY KEY(code, pcf_date)
);
CREATE TABLE IF NOT EXISTS fund_monthly (
  code TEXT NOT NULL, month TEXT NOT NULL, currency TEXT, aum REAL, nav REAL, units REAL,
  subscriptions REAL, redemptions REAL, beneficiaries REAL,
  source TEXT NOT NULL, source_url TEXT NOT NULL, published_at TEXT, PRIMARY KEY(code,month)
);
-- 證交所有價證券集中交易市場開（休）市日期：官方表列出的日子（休市日、開始／最後交易日），上市、上櫃共用
CREATE TABLE IF NOT EXISTS market_calendar (
  date        TEXT PRIMARY KEY,   -- YYYY-MM-DD
  is_open     INTEGER NOT NULL,   -- 1＝當天有交易（開始交易日、最後交易日），0＝休市或僅辦理結算交割
  name        TEXT,               -- 官方名稱，例如「國慶日」「農曆春節前最後交易日」
  description TEXT,               -- 官方說明
  source_url  TEXT,
  fetched_at  TEXT                -- 取得時間
);
-- 官方開休市日期涵蓋的年度；不在這張表的年份一律視為未知，不能把沒有資料的日子當成休市
CREATE TABLE IF NOT EXISTS market_calendar_year (
  year        INTEGER PRIMARY KEY,
  entries     INTEGER,            -- 該年度官方表列出的筆數
  source_url  TEXT,
  fetched_at  TEXT
);
CREATE TABLE IF NOT EXISTS fund_event (
  code TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL, description TEXT,
  source TEXT NOT NULL, source_url TEXT NOT NULL, PRIMARY KEY(code,date,kind)
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
