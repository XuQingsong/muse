"""
打分卡验证 v1（只验行情/资金因子部分，不含基本面）
做法: 对一篮子美股科技股，每21个交易日算一次当时分数（只用当日及以前数据），
再看之后21日/63日的实际涨跌，按高分/中分/低分组对比，并与SPY同期比较。
说明: 篮子是事后挑选的知名科技股，存在幸存者偏差；样本小，只作初步检验，不构成投资建议。
"""
import json, math, subprocess
import pandas as pd
import numpy as np

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

def fetch(url):
    return subprocess.run(["curl","-s","--max-time","40","-A",UA,"-H","Accept: application/json",url],
                          capture_output=True, text=True).stdout

def get_history(t):
    ac = "etf" if t in {"SPY","QQQ"} else "stocks"
    rows = json.loads(fetch(f"https://api.nasdaq.com/api/quote/{t}/historical?assetclass={ac}&fromdate=2022-01-01&limit=99999"))["data"]["tradesTable"]["rows"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close","open","high","low"]:
        df[c] = df[c].str.replace("$","",regex=False).astype(float)
    df["volume"] = pd.to_numeric(df["volume"].str.replace(",","",regex=False), errors="coerce").fillna(0)
    return df.set_index("date").sort_index()

def rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100/(1+gain/loss)

def price_score(df, i):
    """在第i个交易日收盘时算分，与 factor_scorecard_v2 的行情因子口径一致"""
    c, v = df["close"], df["volume"]
    if i < 260: return None
    last = c.iloc[i]
    ma50 = c.iloc[i-49:i+1].mean(); ma200 = c.iloc[i-199:i+1].mean()
    mom3 = (last/c.iloc[i-63]-1)*100; mom6 = (last/c.iloc[i-126]-1)*100
    r = rsi(c.iloc[:i+1]).iloc[-1]
    vol20 = c.iloc[i-19:i+1].pct_change().std()*math.sqrt(252)*100
    dev50 = (last/ma50-1)*100
    vol_ratio = v.iloc[i-19:i+1].mean()/v.iloc[i-59:i+1].mean()
    up = df.iloc[i-59:i+1]
    up_share = up.loc[up["close"]>=up["open"],"volume"].sum()/up["volume"].sum()*100
    s = 0.0
    s += 1 if last>ma200 else -1
    s += 1 if ma50>ma200 else -1
    s += 1 if mom3>10 else (-1 if mom3<-5 else 0)
    s += 1 if mom6>15 else (-1 if mom6<-5 else 0)
    s += 1 if vol_ratio>1.2 else (-0.5 if vol_ratio<0.8 else 0)
    s += 1 if up_share>55 else (-1 if up_share<45 else 0)
    s += -1 if r>70 else (1 if r<30 else 0)
    s += -1 if dev50>10 else (0.5 if abs(dev50)<=3 else 0)
    s += -0.5 if vol20>60 else 0
    return s

TICKERS = ["AAPL","MSFT","NVDA","MRVL","TSLA","VST","AVGO","META","AMZN","GOOGL"]
spy = get_history("SPY")
rows = []
for t in TICKERS:
    try:
        df = get_history(t)
    except Exception as e:
        print(f"{t} 取数失败 {e}"); continue
    # 与SPY对齐共同交易日
    idx = df.index.intersection(spy.index)
    df, s = df.loc[idx], spy.loc[idx]
    for i in range(260, len(df)-63, 21):
        sc = price_score(df, i)
        if sc is None: continue
        f21 = (df["close"].iloc[i+21]/df["close"].iloc[i]-1)*100
        f63 = (df["close"].iloc[i+63]/df["close"].iloc[i]-1)*100
        b21 = (s["close"].iloc[i+21]/s["close"].iloc[i]-1)*100
        b63 = (s["close"].iloc[i+63]/s["close"].iloc[i]-1)*100
        rows.append({"ticker":t,"date":df.index[i].date(),"score":sc,
                     "fwd21":f21,"fwd63":f63,"excess21":f21-b21,"excess63":f63-b63})

res = pd.DataFrame(rows)
res.to_csv("score_validation_results.csv", index=False)
print(f"样本: {len(res)} 次打分, {res['ticker'].nunique()} 只股票, {res['date'].min()} ~ {res['date'].max()}")
def bucket(s):
    return "高分(>=3)" if s>=3 else ("低分(<=0)" if s<=0 else "中分(0~3)")
res["bucket"] = res["score"].apply(bucket)
for b, g in res.groupby("bucket"):
    print(f"\n{b}: n={len(g)}")
    print(f"  之后1个月: 平均 {g['fwd21'].mean():+.1f}% | 跑赢SPY {(g['excess21']>0).mean()*100:.0f}% 的次数 | 超额平均 {g['excess21'].mean():+.1f}%")
    print(f"  之后3个月: 平均 {g['fwd63'].mean():+.1f}% | 跑赢SPY {(g['excess63']>0).mean()*100:.0f}% 的次数 | 超额平均 {g['excess63'].mean():+.1f}%")
print("\n分段相关性(分数 vs 之后涨跌, Spearman):")
print(res[["score","fwd21","fwd63"]].corr(method="spearman").round(2).to_string())
# 时间切分: 前半段 vs 后半段（粗略样本外）
mid = sorted(res["date"])[len(res)//2]
for label, g in [("前半段", res[res["date"]<=mid]), ("后半段", res[res["date"]>mid])]:
    hi, lo = g[g["bucket"]=="高分(>=3)"], g[g["bucket"]=="低分(<=0)"]
    if len(hi) and len(lo):
        print(f"{label}: 高分组3个月超额 {hi['excess63'].mean():+.1f}% (n={len(hi)}) vs 低分组 {lo['excess63'].mean():+.1f}% (n={len(lo)})")
