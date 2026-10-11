# quant 库交接包（供后续单独项目直接上手）

生成时间：2026-10-08。口径：以下行数与日期范围均为当日用 `db.py` 只读回查的实际值，不是文档自报数。

## 1. 怎么连

- PostgreSQL 16.2，库名 `quant`，数据目录 `~/workspace/quant-practice/pgdata`
- 连接信息文件：`~/workspace/quant-practice/DB_CONNECTION.txt`，当前值：
  `QUANT_PG_URI=postgresql://postgres:@/quant?host=/home/hatch/workspace/quant-practice/pgdata`
- Python 入口：`~/workspace/quant-practice/db.py`，`get_conn()` 会自动拉起服务器（基于 pgserver），自建项目可直接 import 使用，或按 URI 用 psycopg / SQLAlchemy 自连。

## 2. 现有表清单（实查）

21 张表。2026-10-08 晚已按三层 schema 分放：data 17 张（下方 estimates_*、
prices_daily、vix_daily、etf_options_daily、options_snapshot_daily、
stock_iv_panel、volume_factors_daily、insider_*、eightk_filings、
short_interest、financial_facts）、strategy 2 张（paper_trades、paper_equity）、
research 2 张（pit_book_scores、raw_json）。连接后先
`SET search_path TO data, strategy, research, public;` 即可裸写表名
（db.py 已内置）。分层后以 `quant_dump_v2_2026-10-08.sql.gz` 为准还原。

- `estimates_snapshot` 30 行 — yfinance 预期快照主表，今起按 `snapshot_date` 追加；目前只有 2026-10-08 一天
- `estimates_trend` 120 行 — 0q/+1q/0y/+1y 四个期间的共识与 7/30/60/90 天前回望值
- `estimates_revisions` 60 行 — 近 7/30 天上调/下调家数
- `price_targets` 30 行 — 目标价 current/mean/median/high/low
- `earnings_history` 119 行 — 最近季度实际 EPS vs 预估与超预期幅度（yfinance 只给近 4 季左右）
- `grade_actions` 19,623 行 — Yahoo 评级动作全史（2011 起）；已验：升级后 63 日中位超额与降级后同为约 +4.97%，在当前 30 只赢家池内无方向区分度
- `prices_daily` 75,728 行，32 只，1999-11-01 ~ 2026-10-07 — 日线 OHLCV（`adj_close` 可用）
- `vix_daily` 9,289 行，1990-01-02 ~ 2026-10-07
- `etf_options_daily` 8,214 行 — SPY/QQQ 市场级 P/C 与 IV 日频聚合（源为 GitHub 数据集，2008-2025 为主）
- `options_snapshot_daily` 32 行 — CBOE 延迟链当日快照（IV30、P/C 量比/持仓比、25Δ 偏度）；自 2026-10-08 起靠每日积累，无历史回测
- `stock_iv_panel` 3,161,661 行 — HuggingFace `options-IV-SP500` 个股逐日 IV 面板，2019-10-14 ~ 2023-07-28；与 `options_snapshot_daily` 之间 2023-07 之后断档，待 IBKR 或继续自积累补齐
- `volume_factors_daily` 28,320 行，30 只，2023-01-03 ~ 2026-10-07 — 量比/涨跌量比/派发日/OBV 背离等逐日量能因子；已验合成量能分无预测力（Spearman 约 -0.057）
- `insider_transactions` 18,335 行；`insider_summary` 30 行 — SEC Form 4；已验内部人公开市场买入在申报后 21/63 日有弱正向超额，高管买入更强，106 天以上衰减明显
- `eightk_filings` 1,790 行，2023-01-03 ~ 2026-10-06 — 8-K 申报与 EX-99 附件定位结果，含指引方向提取（上调/首次给出/维持/下调/撤回）；已验：仅事项代码无预测力，附件指引方向作背景标记（上调后 63 日中位超额约 +5.98%，样本 n=26）
- `short_interest` 364 行 — Nasdaq 卖空（双周，现仅 Nasdaq 上市子集）；已验卖空回补天数 ≥3 时 21 日超额偏弱，样本薄，仅作背景
- `financial_facts` 69,434 行 — PIT 财报事实：一行 = (ticker, concept/tag, period, form, filed, val)，同一报告期多版本并存，`filed` 最早 2009-05-07
- `pit_book_scores` 2,625 行，30 只 × 88 个月末，2019-06-28 ~ 2026-09-30 — 用 financial_facts 按 `filed <= asof_date` 回算的质量/成长/估值三本账分数（`raw` 为 jsonb 原始指标）
- `paper_trades` 1,437 行；`paper_equity` 3,456 行 — 纸面模拟各版本交易与净值；定版为 `paper_trade_v18full`（+49.45%，最大回撤 -12.64%，90 笔），前版 `paper_trade_v14full` 为 +45.26% / -10.43%
- `raw_json` 2 行 — 原始 JSON 留存

注意：`schema.sql` 只覆盖预期/行情/期权/量能/内部人/8-K 等表；`financial_facts` 与 `pit_book_scores` 的建表在 `validate_three_books.py` / `fetch_financial_facts.py` 里，不在 schema.sql，后续项目勿只读 schema.sql 就以为表不存在。

## 3. PIT 查询口径（照这个写，别用最新修订值回测历史）

基本面打分必须按申报日截断：对任意 `asof_date`，同一 (ticker, tag, period_end) 只取 `filed <= asof_date` 的最新一条。示例：

```sql
SELECT DISTINCT ON (ticker, tag, period_end)
  ticker, tag, period_end, val, filed
FROM financial_facts
WHERE filed <= DATE '2021-12-31'
ORDER BY ticker, tag, period_end, filed DESC;
```

三本账现成截面直接查 `pit_book_scores`（`asof_date` 为月末），需要日频或新口径时再回 financial_facts 重算。

## 4. 每日任务现状

- cron `daily-estimates-snapshot` 已启用，每天 10:17 (Asia/Shanghai) 运行 `fetch_estimates_daily.py`，把当日预期快照写入上面 6 张预期相关表。
- 2026-10-08 已实查入库 30 只 × 当日一行；cron 历史运行记录尚为空，属正常（首次定时触发为 2026-10-09 10:17）。
- 任务设定为成功静默、失败才报告；积满约 90 天后才能用自存序列验「上修组 vs 下修组」，此前只能用 Yahoo 的 90 天回望值（注意财年滚动会污染 rev90_pct，净修订家数更可信）。

## 5. 已验证结论速查（细节见 FRAMEWORK_V3.md / signal_validation_report.md）

- 旧等权总分：420 个历史时点验证后与后续 63 日相对涨幅相关约 -0.109，已否定；技术因子只作入场位置与风险闸门。
- 质量账无效（63 日 IC -0.016）；成长账总分无效，但营收增速加速度单项两段同向有效（IC +0.093）；估值账弱有效（IC +0.064，便宜组 63 日中位 +1.56% vs 贵组 -0.71%）。用户 2026-10-08 拍板：先不动框架，结论留档。
- 内部人买入、VIX 恐慌闸门（已接入 v18 定版）；量能合成、Yahoo 评级方向、市场级 P/C 叠加，均已验为无效或冗余，不进规则。
- 全部模拟结论来自 30 只大盘赢家池 + 2026 年单窗口 + 单次恐慌簇，无 2022 式阴跌年样本外检验；MU 单票集中贡献约六成，回撤仍在 10% 量级。

## 6. 已知缺口（写新项目前先认）

- 个股期权：2023-07-28 后无历史；只能自积累或接 IBKR，用户已拍板暂不折腾。
- 预期修正：无带日期的历史修订库，只能向前攒；FMP 订阅已明确不买；FinImpulse 仅留评估记录，未接入生产。
- 财报超预期全史：yfinance 只有近 4 季，网络公开可查零散数据，未系统入库。
- 13F 机构持仓、个股期权资金流、新闻情绪分：均无可靠免费历史源，未入库。

## 7. 关键文件索引

- 入口与连接：`db.py`、`DB_CONNECTION.txt`
- 表结构：`schema.sql`（另见 `validate_three_books.py`、`fetch_financial_facts.py` 内建表）
- 框架与结论：`FRAMEWORK_V3.md`、`FRAMEWORK_V10.md`、`signal_validation_report.md`
- 信息面清单：`INFO_SURFACE_PLAN.md`
- 定版模拟：`paper_trade_v18_full30.py`（表内 version=`paper_trade_v18full`）
