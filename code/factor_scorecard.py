"""
因子打分卡（Factor Scorecard）—— 用多类因子给一只美股打分，看入场机会
因子来源（全部可从公开行情算出）：
  趋势: 价格 vs 50/200日均线、50日线 vs 200日线
  动量: 近1/3/6个月涨幅
  量能: 近20日成交量 vs 60日均量、上涨日量能占比
  位置: 距52周高点距离、RSI(14)、距50日线乖离率
  风险: 近20日波动率（年化）、近一年最大回撤
  估值参考: 现价 vs 分析师1年目标价（Nasdaq汇总口径，仅供参考）
使用: python factor_scorecard.py MRVL VST NVDA
说明: 教学与研究辅助，不构成投资建议。基本面因子（PE/ROE/营收增速）需财报数据源，见文末清单。
"""
import sys, json, math, subprocess
import pandas as pd
import numpy as np

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

def nasdaq(path):
    out = subprocess.run(
        ["curl", "-s", "--max-time", "30", "-H", f"User-Agent: {UA}", "-H", "Accept: application/json",
         f"https://api.nasdaq.com/api/{path}"],
        capture_output=True, text=True).stdout
    return json.loads(out)

def get_history(ticker):
    is_etf = ticker in {"SPY", "QQQ", "DIA", "IWM"}
    ac = "etf" if is_etf else "stocks"
    d = nasdaq(f"quote/{ticker}/historical?assetclass={ac}&fromdate=2023-01-01&limit=99999")
    rows = d["data"]["tradesTable"]["rows"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close", "open", "high", "low"]:
        df[c] = df[c].str.replace("$", "", regex=False).astype(float)
    df["volume"] = df["volume"].str.replace(",", "", regex=False).astype(float)
    return df.set_index("date").sort_index()

def get_summary(ticker):
    d = nasdaq(f"quote/{ticker}/summary?assetclass=stocks")
    s = d.get("data", {}).get("summaryData", {}) or {}
    def val(k):
        v = (s.get(k) or {}).get("value")
        return v
    return {"market_cap": val("MarketCap"), "avg_vol": val("AverageVolume"),
            "yr_range": val("FiftTwoWeekHighLow"), "target": val("OneYrTarget"),
            "sector": val("Sector"), "industry": val("Industry")}

def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(n).mean()
    loss = (-delta.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + gain / loss)

def score_ticker(t):
    df = get_history(t)
    c, v = df["close"], df["volume"]
    last = c.iloc[-1]
    ma50, ma200 = c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1]
    mom = {f"{m}月": (last / c.iloc[-21 * m] - 1) * 100 for m in (1, 3, 6)}
    r = rsi(c).iloc[-1]
    vol20 = c.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100
    hi52 = c.tail(252).max()
    dd = ((c / c.cummax()) - 1).tail(252).min() * 100
    vol_ratio = v.tail(20).mean() / v.tail(60).mean()
    up = df.tail(60)
    up_vol_share = up.loc[up["close"] >= up["open"], "volume"].sum() / up["volume"].sum() * 100
    dev50 = (last / ma50 - 1) * 100

    f = []  # (因子, 数值, 判定, 分数-2..+2)
    f.append(("趋势·价vs200日线", f"现价{last:.2f} / 200日线{ma200:.2f}", "上方" if last > ma200 else "下方", 1 if last > ma200 else -1))
    f.append(("趋势·50日线vs200日线", f"{ma50:.2f} vs {ma200:.2f}", "多头排列" if ma50 > ma200 else "空头排列", 1 if ma50 > ma200 else -1))
    f.append(("动量·3个月涨幅", f"{mom['3月']:+.1f}%", "强" if mom["3月"] > 10 else ("弱" if mom["3月"] < -5 else "中"), 1 if mom["3月"] > 10 else (-1 if mom["3月"] < -5 else 0)))
    f.append(("动量·6个月涨幅", f"{mom['6月']:+.1f}%", "强" if mom["6月"] > 15 else ("弱" if mom["6月"] < -5 else "中"), 1 if mom["6月"] > 15 else (-1 if mom["6月"] < -5 else 0)))
    f.append(("量能·20日量/60日量", f"{vol_ratio:.2f}倍", "放量" if vol_ratio > 1.2 else ("缩量" if vol_ratio < 0.8 else "正常"), 1 if vol_ratio > 1.2 else (0 if vol_ratio >= 0.8 else -0.5)))
    f.append(("量能·上涨日量能占比(60日)", f"{up_vol_share:.0f}%", "买盘主导" if up_vol_share > 55 else ("卖盘主导" if up_vol_share < 45 else "均衡"), 1 if up_vol_share > 55 else (-1 if up_vol_share < 45 else 0)))
    f.append(("位置·距52周高点", f"{(last/hi52-1)*100:+.1f}%", "贴近高点" if last / hi52 > 0.95 else "有距离", 0.5 if last / hi52 > 0.95 else 0))
    f.append(("位置·RSI(14)", f"{r:.0f}", "过热(>70)" if r > 70 else ("超卖(<30)" if r < 30 else "中性"), -1 if r > 70 else (1 if r < 30 else 0)))
    f.append(("位置·距50日线乖离", f"{dev50:+.1f}%", "追高风险" if dev50 > 10 else ("回踩均线附近" if abs(dev50) <= 3 else "正常"), -1 if dev50 > 10 else (0.5 if abs(dev50) <= 3 else 0)))
    f.append(("风险·20日波动率(年化)", f"{vol20:.0f}%", "高波动" if vol20 > 40 else "平稳", -0.5 if vol20 > 60 else 0))
    f.append(("风险·近一年最大回撤", f"{dd:.0f}%", "深回撤" if dd < -40 else "可控", 0))

    total = sum(x[3] for x in f)
    sm = get_summary(t)
    print(f"\n===== {t} 因子打分卡（数据截至 {df.index[-1].date()}）=====")
    if sm.get("sector"): print(f"行业: {sm['sector']} / {sm['industry']}；市值: {sm['market_cap']}；分析师1年目标价: {sm['target']}")
    for name, val, judge, s in f:
        print(f"  {name:<24} {val:<28} {judge:<12} {s:+.1f}")
    print(f"  —— 综合分（满分约+9）: {total:+.1f}")
    # 入场观察（规则透明、仅供研究参考）
    notes = []
    if last > ma200 and ma50 > ma200: notes.append("大趋势向上")
    if dev50 > 10: notes.append("离50日线太远，追高风险大，等回踩")
    elif abs(dev50) <= 3 and last > ma200: notes.append("回踩到50日线附近且趋势未破，是量化里常见的观察入场位")
    if r > 70: notes.append("RSI过热，短期别追")
    if vol_ratio > 1.2 and mom["1月"] > 0: notes.append("放量上涨，量价配合")
    if last < ma200: notes.append("在200日线下方，中长线趋势未确认，先观察")
    print("  入场观察: " + ("；".join(notes) if notes else "无明显信号，中性观望"))

if __name__ == "__main__":
    for t in (sys.argv[1:] or ["MRVL"]):
        try:
            score_ticker(t.upper())
        except Exception as e:
            print(f"{t}: 取数或计算失败（{type(e).__name__}）")
