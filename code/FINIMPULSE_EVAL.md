# FinImpulse 核查（2026-10-08，研究阶段，未实测其 API）

背景：用户 2026-10-08 12:42（本地）用 Google 账号登录 FinImpulse 并收到账号凭据邮件（Gmail source:41 / source:40）。此前预期面缺口已确认：TradingView 收费版、FMP Starter、iFinD、Tushare 均补不了；唯一对口原为 FMP Premium 59 美元/月（年付）。

## 它有什么（据官方文档/定价页，2026-10-08 查）
- /analysis/earnings：EPS/营收实际 vs 估计、surprise%，及 eps_trend（现值 vs 7/30/60/90 天前）、eps_revisions（近 7/30 天上修/下修家数）、分析师人数与估计区间。支持 start/end 日期与类型筛选。
- /analysis/upgrades-downgrades：逐条评级动作，grade_date 带时分秒、机构名、from/to 评级、目标价前后值——天然 PIT，可直接做事件验证。
- /analysis/recommendations：按月快照的 Strong Buy/Buy/Hold/Sell 家数序列。
- 另有 /news、/options/chain（现链：量/OI/IV，无历史迹象）、机构与内部人持仓（与 SEC 免费源重复）。
- 定价按量：分析类接口每次调用 0.0008 美元 + 每行 0.00015 美元；含沙盒与 1 美元测试额度。官网：https://finimpulse.com/api/pricing/ ，文档：https://developers.finimpulse.com/v1/analysis/earnings/ 等。

## 成本估算（假设每票返回行数，未实测）
- 试点 4 只（NVDA/MU/PLTR/UNH）× 三个分析接口：约 0.1 美元内，1 美元测试额度足够。
- 全池 32 只首拉（预期+评级动作+推荐快照）：约 1–3 美元一次性；之后自存快照滚动，增量为分级。
- 对照 FMP Premium 59 美元/月年付（约 708 美元/年）。

## 风险与未验证点（必须试点后才算数）
- 公司 2026 年成立、团队 2–10 人（LinkedIn），数据来源未披露，字段风格疑似聚合公开源；历史深度与准确性未实测。
- earnings 的 date 是报告期而非修订时点：是否给真正的逐时点预期修正序列未证实；eps_trend 只是当前截面的 90 天回望，向前靠自存快照才成 PIT 库。
- 期权链看起来只有现链、无历史归档，个股期权历史缺口它大概率解决不了（同 CBOE 免费源定位）。
- 基本面/内部人/机构持仓与现有 SEC 免费管道重复，不必为此付费。

## 建议试点（待用户拍板，勿先行）
1. 用测试额度拉 4 只试点票三接口，验：最早日期、行数、实际花费、修订字段是否为真时点序列。
2. 达标再扩 32 只并落 PIT 快照表；不达标则维持 FMP Premium 为唯一对口项再议。
3. 凭据纪律：不要把密钥/口令贴进聊天，走此前 FMP 的安全录入方式；FinImpulse 邮件把系统生成的口令明文发到了邮箱，建议用户在 Profile → Security 改成自设口令（邮件原文未在此复述）。
