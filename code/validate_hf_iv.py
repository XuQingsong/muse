"""HF 个股期权 IV 面板验证：options-IV-SP500（2019-10~2023-07，3893 只）
是否能预测个股相对 SPY 的超额收益——只看池内 32 只。
IV 来自按虚实分档的 ATM_IV，P/C 来自合约成交量。每 21 个交易日取样以降
窗口重叠。口径：信号日次日入场，相对 SPY 超额，分 2020-21 / 2022-23 两段。
"""
import numpy as np
import pandas as pd
import yfinance as yf

TICKERS = ["MU", "GOOGL", "NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO",
           "AMD", "MRVL", "PLTR", "NOW", "ORCL", "CRM", "NFLX", "TSLA",
           "ADBE", "COST", "WMT", "HD", "UNH", "LLY", "JNJ", "V", "MA",
           "JPM", "XOM", "CVX", "CAT", "GE"]

iv = pd.read_csv("hf_stock_iv/data_IV_USA.csv",
                 usecols=["symbol", "date", "calls_contracts_traded",
                          "puts_contracts_traded", "ATM_IV", "hv_20"])
iv["date"] = pd.to_datetime(iv["date"])
iv = iv[iv["symbol"].isin(TICKERS)].copy()
iv["pc_share"] = iv["puts_contracts_traded"] / (
    iv["puts_contracts_traded"] + iv["calls_contracts_traded"])
iv["iv_prem"] = iv["ATM_IV"] - iv["hv_20"]
print(f"IV 面板：{len(iv)} 行，{iv['symbol'].nunique()} 只，"
      f"{iv['date'].min().date()}~{iv['date'].max().date()}")

px = yf.download(TICKERS + ["SPY"], start="2019-09-01", end="2023-10-15",
                 auto_adjust=True, progress=False)["Close"]
spy = px["SPY"]

def fwd_excess(ret_t, t, date, h):
    """date 之后第 1 个交易日入场，持有 h 日的相对 SPY 超额（%）。"""
    s_ret = px[t]
    i = s_ret.index.searchsorted(date, side="right")
    j = spy.index.searchsorted(date, side="right")
    if i + h >= len(s_ret) or j + h >= len(spy):
        return None
    return ((s_ret.iloc[i + h] / s_ret.iloc[i] - 1)
            - (spy.iloc[j + h] / spy.iloc[j] - 1)) * 100

rows = []
for t, g in iv.groupby("symbol"):
    g = g.sort_values("date").reset_index(drop=True)
    g["iv_rank"] = g["ATM_IV"].rolling(252, min_periods=120).rank(pct=True)
    g = g.iloc[::21]  # 每月取样
    for _, r in g.iterrows():
        for h in (21, 63):
            v = fwd_excess(None, t, r["date"], h)
            if v is not None:
                rows.append({"t": t, "date": r["date"], "h": h, "ex": v,
                             "iv_rank": r["iv_rank"], "iv_prem": r["iv_prem"],
                             "pc_share": r["pc_share"]})
ev = pd.DataFrame(rows)
ev["sub"] = np.where(ev["date"] < "2022-01-01", "2020-21", "2022-23")

def stats(mask, label):
    for h in (21, 63):
        x = ev[(ev["h"] == h) & mask(ev)]
        if len(x) < 10:
            continue
        subs = []
        for s in ("2020-21", "2022-23"):
            xs = x[x["sub"] == s]["ex"]
            if len(xs) >= 5:
                subs.append(f"{s} n={len(xs)} 中位{xs.median():+.2f}%")
        print(f"{label} {h}日 n={len(x)} 中位{x['ex'].median():+.2f}% "
              f"胜率{(x['ex'] > 0).mean() * 100:.0f}% | " + "；".join(subs))

print("\n=== 个股 IV / 期权情绪 -> 相对 SPY 超额 ===")
stats(lambda d: d["iv_rank"] >= 0.8, "IV高位(≥80分位)")
stats(lambda d: d["iv_rank"] <= 0.2, "IV低位(≤20分位)")
stats(lambda d: d["iv_prem"] >= 10, "IV溢价高(IV-HV≥10)")
stats(lambda d: d["iv_prem"] <= 0, "IV不贵(IV≤HV)")
stats(lambda d: d["pc_share"] >= 0.5, "Put成交占多数")
stats(lambda d: d["pc_share"] <= 0.25, "Call成交主导")
stats(lambda d: d["iv_rank"].notna(), "基线(全部)")
ev.to_csv("hf_iv_events.csv", index=False)
