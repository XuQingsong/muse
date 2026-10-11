"""
技术因子单项验证（为新版“四本账”框架校准时机账）
- 每21个交易日取一个截面，只用当日及以前数据
- 看之后21/63个交易日相对SPY的超额收益
- 输出每个因子的Spearman IC、最高/最低三分位平均超额
注意：10只知名科技/能源股的事后篮子，有幸存者偏差；样本小，只用于判断因子方向，
不能当成可交易策略或收益承诺。
"""
import json, math, subprocess
import numpy as np
import pandas as pd

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TICKERS = ["AAPL", "MSFT", "NVDA", "MRVL", "TSLA", "VST", "AVGO", "META", "AMZN", "GOOGL"]


def fetch(url):
    return subprocess.run(
        ["curl", "-s", "--max-time", "40", "-A", UA, "-H", "Accept: application/json", url],
        capture_output=True, text=True,
    ).stdout


def get_history(t):
    ac = "etf" if t in {"SPY", "QQQ"} else "stocks"
    raw = json.loads(fetch(f"https://api.nasdaq.com/api/quote/{t}/historical?assetclass={ac}&fromdate=2022-01-01&limit=99999"))
    rows = raw["data"]["tradesTable"]["rows"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close", "open", "high", "low"]:
        df[c] = df[c].str.replace("$", "", regex=False).astype(float)
    df["volume"] = pd.to_numeric(df["volume"].str.replace(",", "", regex=False), errors="coerce").fillna(0)
    return df.set_index("date").sort_index()


def rsi_series(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + gain / loss)


def factors_at(df, i):
    c, v = df["close"], df["volume"]
    if i < 260:
        return None
    last = c.iloc[i]
    ma50 = c.iloc[i - 49:i + 1].mean()
    ma200 = c.iloc[i - 199:i + 1].mean()
    ma200_prev = c.iloc[i - 219:i - 19].mean() if i >= 219 else np.nan
    window = df.iloc[i - 59:i + 1]
    up_share = window.loc[window["close"] >= window["open"], "volume"].sum() / max(window["volume"].sum(), 1) * 100
    return {
        "price_vs_ma200_pct": (last / ma200 - 1) * 100,
        "ma50_vs_ma200_pct": (ma50 / ma200 - 1) * 100,
        "ma200_slope_20d_pct": (ma200 / ma200_prev - 1) * 100,
        "mom_3m_pct": (last / c.iloc[i - 63] - 1) * 100,
        "mom_6m_pct": (last / c.iloc[i - 126] - 1) * 100,
        "volume_ratio_20_60": v.iloc[i - 19:i + 1].mean() / v.iloc[i - 59:i + 1].mean(),
        "up_volume_share_60_pct": up_share,
        "rsi14": rsi_series(c.iloc[:i + 1]).iloc[-1],
        "distance_ma50_pct": (last / ma50 - 1) * 100,
        "volatility_20d_ann_pct": c.iloc[i - 19:i + 1].pct_change().std() * math.sqrt(252) * 100,
        "drawdown_252d_pct": (last / c.iloc[i - 251:i + 1].max() - 1) * 100,
    }


def main():
    spy = get_history("SPY")
    rows = []
    for t in TICKERS:
        df = get_history(t)
        idx = df.index.intersection(spy.index)
        df, s = df.loc[idx], spy.loc[idx]
        for i in range(260, len(df) - 63, 21):
            f = factors_at(df, i)
            if f is None:
                continue
            f.update({
                "ticker": t,
                "date": df.index[i].date(),
                "excess21": (df["close"].iloc[i + 21] / df["close"].iloc[i] - s["close"].iloc[i + 21] / s["close"].iloc[i]) * 100,
                "excess63": (df["close"].iloc[i + 63] / df["close"].iloc[i] - s["close"].iloc[i + 63] / s["close"].iloc[i]) * 100,
            })
            rows.append(f)
    res = pd.DataFrame(rows)
    res.to_csv("technical_factor_samples.csv", index=False)
    factor_cols = [c for c in res.columns if c not in {"ticker", "date", "excess21", "excess63"}]
    out = []
    mid = sorted(res["date"])[len(res) // 2]
    for col in factor_cols:
        d = res[[col, "excess21", "excess63", "date"]].dropna()
        try:
            qs = pd.qcut(d[col], 3, labels=["low", "mid", "high"], duplicates="drop")
        except ValueError:
            continue
        d = d.assign(bucket=qs)
        g = d.groupby("bucket", observed=True).agg(
            n=("excess63", "size"),
            ex21=("excess21", "mean"),
            ex63=("excess63", "mean"),
        )
        early = d[d["date"] <= mid]
        late = d[d["date"] > mid]
        out.append({
            "factor": col,
            "n": len(d),
            "ic21": d[col].corr(d["excess21"], method="spearman"),
            "ic63": d[col].corr(d["excess63"], method="spearman"),
            "ic63_first_half": early[col].corr(early["excess63"], method="spearman") if len(early) > 20 else np.nan,
            "ic63_second_half": late[col].corr(late["excess63"], method="spearman") if len(late) > 20 else np.nan,
            "low_ex63": g.loc["low", "ex63"] if "low" in g.index else np.nan,
            "high_ex63": g.loc["high", "ex63"] if "high" in g.index else np.nan,
            "high_minus_low_ex63": (g.loc["high", "ex63"] - g.loc["low", "ex63"]) if {"high", "low"}.issubset(g.index) else np.nan,
        })
    summary = pd.DataFrame(out).sort_values("ic63", ascending=False)
    summary.to_csv("technical_factor_validation_summary.csv", index=False)
    print(f"样本 {len(res)} 个截面，{res['ticker'].nunique()} 只股票，{res['date'].min()} ~ {res['date'].max()}")
    print(summary.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
