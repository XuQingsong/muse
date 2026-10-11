"""
第一份量化纸面练习：双均线策略 vs 直接买宽基指数 ETF
- 框架: vectorbt（OSS, 仍在维护）
- 数据: SPY 日线收盘价（Yahoo Finance）
- 流程: 样本内(2015-2019)选参 -> 样本外(2020-2025)验证 -> 与买入持有对比
- 成本口径: 佣金+滑点各 0.05%（故意填高一点）
说明: 教学演示，不构成投资建议。
"""
import vectorbt as vbt
import pandas as pd

SYMBOL = "SPY"
INIT_CASH = 100_000
FEES = 0.0005       # 单边佣金 0.05%
SLIPPAGE = 0.0005   # 单边滑点 0.05%

# 1) 数据：从本地 Nasdaq 历史 JSON 读取（2015-01-01 起）
import json
with open("spy_nasdaq.json") as f:
    rows = json.load(f)["data"]["tradesTable"]["rows"]
df = pd.DataFrame(rows)
df["date"] = pd.to_datetime(df["date"])
price = df.set_index("date")["close"].str.replace("$", "", regex=False).astype(float).sort_index()
price = price.loc["2015-01-01":"2025-12-31"].dropna()
print(f"数据: {SYMBOL} {price.index[0].date()} ~ {price.index[-1].date()}, 共 {len(price)} 个交易日")

train = price.loc["2015":"2019"]
test = price.loc["2020":"2025"]

def run(price, fast, slow, fees, slippage):
    fast_ma = vbt.MA.run(price, fast)
    slow_ma = vbt.MA.run(price, slow)
    entries = fast_ma.ma_crossed_above(slow_ma)
    exits = fast_ma.ma_crossed_below(slow_ma)
    pf = vbt.Portfolio.from_signals(
        price, entries, exits, init_cash=INIT_CASH, fees=fees, slippage=slippage, freq="1D"
    )
    return pf

def perf(pf):
    s = pf.stats()
    return {
        "总收益%": round(float(s["Total Return [%]"]), 2),
        "最大回撤%": round(float(s["Max Drawdown [%]"]), 2),
        "夏普": round(float(s["Sharpe Ratio"]), 2),
        "交易次数": int(s["Total Trades"]),
        "胜率%": round(float(s["Win Rate [%]"]), 2),
    }

# 2) 样本内选参（2015-2019）: fast in [5,10,20], slow in [50,100,200]
print("\n== 样本内选参（2015-2019，含成本）==")
results = {}
for fast in [5, 10, 20]:
    for slow in [50, 100, 200]:
        if fast >= slow: continue
        r = perf(run(train, fast, slow, FEES, SLIPPAGE))
        results[(fast, slow)] = r["总收益%"]
        print(f"fast={fast:>2} slow={slow:>3}  样本内总收益 {r['总收益%']:>8}%  交易{r['交易次数']}次")

best = max(results, key=results.get)
print(f"样本内最优参数: fast={best[0]}, slow={best[1]}（样本内收益 {results[best]}%）")

# 3) 基准：直接买并持有（样本内）
bh_train = vbt.Portfolio.from_holding(train, init_cash=INIT_CASH, fees=FEES, freq="1D")
print(f"\n买入持有 SPY（2015-2019）: {perf(bh_train)}")

# 4) 样本外验证（2020-2025）：用样本内选出的参数
print("\n== 样本外验证（2020-2025，含成本）==")
pf_out = run(test, best[0], best[1], FEES, SLIPPAGE)
print(f"策略 {best}: {perf(pf_out)}")

pf_out_free = run(test, best[0], best[1], 0.0, 0.0)
print(f"策略 {best}（零成本对比）: {perf(pf_out_free)}")

bh_test = vbt.Portfolio.from_holding(test, init_cash=INIT_CASH, fees=FEES, freq="1D")
print(f"买入持有 SPY（2020-2025）: {perf(bh_test)}")
