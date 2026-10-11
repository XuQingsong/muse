# stock-pm 同步方案（2026-10-10，待用户拍板）

背景：2026-10-10 Codex 核对发现沙箱与 Mac 无跨环境同步；沙箱产物在 `~/workspace/quant-practice/paper_live/`，Mac 仍为 10/8 版、无 paper_tracker/paper_live。本方案只给可选路径，不自动改任务、不执行交易。

## 已核实事实（2026-10-10 本地代码只读检查）
- 沙箱现持仓状态：`paper_live/state.json`（10 只，现金 562160.78 美元，含 PLTR 201 股）；最近结算样例：`paper_live/reports/settle_2026-10-09.json`（events 为空，即该结算日无止损/止盈/买入成交记录）。
- 最新新买扫描：`today_plan_2026-10-10.json`（signal_asof 2026-10-09）plan 为 9 只、不含 PLTR。注意：这是“从零新买”视角，不等于要卖出已持有的 PLTR；前向跟踪器只在止损/止盈/基本面死亡时退出已持仓。
- `paper_tracker.py` 的 premarket/settle 只依赖行情/财报网络取数与 yfinance 分钟线（见 `paper_tracker.py` 中 `latest_signal`/`_download_intraday`/`_download_daily` 调用），不依赖本地 PostgreSQL 的 quant 库；每日预期快照写库是另一条线（`fetch_estimates_daily.py`），与网页展示可分开。
- 现有自动任务在沙箱运行：盘前 `paper-tracker-premarket`（周一至周五 20:55 CST，下次 2026-10-12）、盘后 `paper-tracker-settlement`（周二至周六 09:35 CST，下次 2026-10-13）。若改到 Mac 本地跑，必须先停沙箱两条，避免双跑分叉。

## 推荐路径 A：执行迁到 Mac 本地（网页可直接读本地文件）
1. 在 Mac 解压已下载的 10/10 代码包到 `~/workspace/quant-practice/`（含 `paper_tracker.py`、`today_plan.py`、`paper_trade_v18_full30.py`、`factor_scorecard_v2.py` 等）。
2. 不要在 Mac 上执行 `paper_tracker.py init` 重置（会按 INITIAL_PLAN 重建）。改为把沙箱当前的 `paper_live/state.json` 原样拷到 Mac 同路径，作为唯一账本起点；`paper_live/reports/settle_2026-10-09.json` 一并拷贝作基线。
3. Mac 本地建 venv 后只做只读自检：能 import `paper_tracker`、能读到 state.json 的 10 只与现金数；先不要跑 premarket/settle 写账本。
4. 用户确认切换后：先停用沙箱两条 cron，再在 Mac 用 launchd/手动各跑一次 premarket 与 settle 验证报告落盘，最后让 stock-pm 只读 `~/workspace/quant-practice/paper_live/state.json` 与 `paper_live/reports/*.json`。
5. 回滚：切换前备份 Mac 旧目录；若本地跑失败，恢复沙箱 cron 即可，账本以最后一次成功结算的 state.json 为准。

## 备选路径 B：继续沙箱执行 + 人工导出包（改动最小）
- 每次需要给 Codex/stock-pm 时，从沙箱打包 `paper_live/state.json`、`paper_live/reports/*.json`、`today_plan_*.json` 最新一份为一个 zip 经聊天附件投递；网页按文件时间戳展示，并标注“非实时同步”。
- 缺点：周一盘前/周二结算后都要人工投递一次，易漏；只适合网页先搭前端的过渡期（建议不超过一周）。

## 网页必须标注的口径（避免误读）
- VIX 恐慌闸门与进攻层 -40% 灾难止损仅属 v18 回测，前向 `paper_tracker` 未启用，不要画成前向规则。
- `today_plan.all` 目前只有四本账总分，子项与“差几分”原因未落盘；要展示原因需另跑 `latest_signal` 取 redlines/caps 或后续给扫描加字段（当前标“需补充”）。
- 图表价格线为未复权价；若用 adj_close 绘图，成本/止损/止盈三线需乘 (adj_close/close) 换算，不得混画。

## 需要用户拍板的只有一句
选 A（迁 Mac 本地，网页直读）还是先 B（一周人工导出过渡）？选 A 后我再给 Mac 上可直接粘贴的命令清单；选 B 则下周一盘前报告出来后我打包投递。
