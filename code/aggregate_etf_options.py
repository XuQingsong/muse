"""ETF 期权历史聚合+验证：SPY(2008-2025)/QQQ(2011-2025) 逐合约 parquet ->
日频市场级序列（P/C 量比、P/C 持仓比、ATM IV、25Δ 偏度），并验证恐慌型
极值（过去1年90分位）之后 SPY 的 21/63 日收益（次日收盘入场），与 VIX
恐慌闸门对照（重叠度、独立出现时是否仍有效）。数据源：
github.com/danielevansmith/options-dataset-hist (MIT)。
"""
import glob
import numpy as np
import pandas as pd

def aggregate(sym):
    frames = []
    for f in sorted(glob.glob(f"etf_options/{sym}/options_*.parquet")):
        df = pd.read_parquet(f, columns=["date", "expiration", "type", "volume",
                                         "open_interest", "implied_volatility", "delta"])
        frames.append(df)
    d = pd.concat(frames, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d["expiration"] = pd.to_datetime(d["expiration"])
    d["dte"] = (d["expiration"] - d["date"]).dt.days
    vol = d.pivot_table(index="date", columns="type", values="volume", aggfunc="sum")
    oi = d.pivot_table(index="date", columns="type", values="open_interest", aggfunc="sum")
    out = pd.DataFrame(index=vol.index)
    out["pc_vol"] = vol["put"] / vol["call"]
    out["pc_oi"] = oi["put"] / oi["call"]
    out["total_vol"] = vol.sum(axis=1)
    band = d[(d["dte"] >= 20) & (d["dte"] <= 40)]
    atm = band[(band["delta"].abs() >= 0.4) & (band["delta"].abs() <= 0.6)]
    out["iv_atm"] = atm.groupby("date")["implied_volatility"].mean() * 100
    puts = band[(band["type"] == "put") & (band["delta"].abs() >= 0.2) & (band["delta"].abs() <= 0.3)]
    calls = band[(band["type"] == "call") & (band["delta"].abs() >= 0.2) & (band["delta"].abs() <= 0.3)]
    out["skew25"] = (puts.groupby("date")["implied_volatility"].mean()
                     - calls.groupby("date")["implied_volatility"].mean()) * 100
    out["symbol"] = sym.upper()
    return out.dropna(subset=["pc_vol"])

spy = aggregate("spy")
qqq = aggregate("qqq")
hist = pd.concat([spy, qqq]).sort_index()
hist.to_csv("options_etf_history_daily.csv")
print(f"聚合完成：SPY {len(spy)} 天 ({spy.index.min().date()}~{spy.index.max().date()})，"
      f"QQQ {len(qqq)} 天；已写 options_etf_history_daily.csv")
print(spy.tail(3).round(3).to_string())

# ---------- 验证 ----------
px = pd.read_parquet("etf_options/spy/underlying_prices.parquet")
px["date"] = pd.to_datetime(px["date"])
px = px.set_index("date").sort_index()
close = px["adjusted_close"].astype(float)
vix = pd.read_csv("options_vix_history.csv", parse_dates=["date"]).set_index("date")["vix"].sort_index()

def crossings(series, pct=0.90):
    thr = series.rolling(252, min_periods=120).quantile(pct)
    hot = series >= thr
    first = hot & (~hot.shift(1, fill_value=False))
    return list(series.index[first])

def fwd(dates, h):
    out = []
    idx = close.index
    for dt in dates:
        i = idx.searchsorted(dt, side="right")
        if i + h < len(idx):
            out.append((close.iloc[i + h] / close.iloc[i] - 1) * 100)
    return np.array(out)

base21, base63 = fwd(list(close.index[::1]), 21), fwd(list(close.index), 63)
print(f"\n无条件基线：次日入场 SPY 21日 {np.median(base21):+.2f}%（中位） 63日 {np.median(base63):+.2f}%")

for name, series in [("SPY P/C量比", spy["pc_vol"]), ("SPY ATM IV", spy["iv_atm"]), ("SPY P/C持仓比", spy["pc_oi"])]:
    ev = [d for d in crossings(series) if d >= pd.Timestamp("2008-06-01")]
    r21, r63 = fwd(ev, 21), fwd(ev, 63)
    if len(r63):
        print(f"{name} 90分位触发 n={len(r63)}：21日中位 {np.median(r21):+.2f}% 胜率 {(r21 > 0).mean() * 100:.0f}%；"
              f"63日中位 {np.median(r63):+.2f}% 胜率 {(r63 > 0).mean() * 100:.0f}%")

# 与 VIX 的增量关系：P/C 触发中，±5 日内有无 VIX 恐慌
vix_thr = vix.rolling(252, min_periods=120).quantile(0.90)
vix_hot = set(vix.index[vix >= vix_thr])
ev = [d for d in crossings(spy["pc_vol"]) if d >= pd.Timestamp("2008-06-01")]
near, far = [], []
for d in ev:
    window = {d + pd.Timedelta(days=k) for k in range(-5, 6)}
    (near if window & vix_hot else far).append(d)
for label, group in [("P/C恐慌且VIX同步", near), ("P/C恐慌但VIX平静", far)]:
    r = fwd(group, 63)
    if len(r):
        print(f"{label} n={len(r)}：SPY 63日中位 {np.median(r):+.2f}% 胜率 {(r > 0).mean() * 100:.0f}%")
