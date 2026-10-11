"""
因子打分卡 v2 —— 行情因子（Nasdaq公开行情）+ 基本面因子（SEC EDGAR官方财报）+ 宏观背景（FRED）
全部免费源。使用: python factor_scorecard_v2.py MRVL VST
说明: 教学与研究辅助，不构成投资建议。基本面为财报口径、按季更新，有滞后。
"""
import sys, json, math, subprocess
import pandas as pd
import numpy as np

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SEC_UA = "MuseQuantResearch research@example.com"

def fetch(url, ua=UA, accept=None):
    cmd = ["curl", "-s", "--max-time", "40", "-A", ua]
    if accept: cmd += ["-H", f"Accept: {accept}"]
    cmd.append(url)
    return subprocess.run(cmd, capture_output=True, text=True).stdout

# ---------- 行情（Nasdaq） ----------
def nasdaq(path):
    return json.loads(fetch(f"https://api.nasdaq.com/api/{path}", accept="application/json"))

def get_history(t):
    ac = "etf" if t in {"SPY", "QQQ", "DIA", "IWM"} else "stocks"
    rows = nasdaq(f"quote/{t}/historical?assetclass={ac}&fromdate=2023-01-01&limit=99999")["data"]["tradesTable"]["rows"]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["close", "open", "high", "low"]:
        df[c] = df[c].str.replace("$", "", regex=False).str.replace(",", "", regex=False).astype(float)
    df["volume"] = df["volume"].str.replace(",", "", regex=False).str.replace("N/A", "0", regex=False).astype(float)
    return df.set_index("date").sort_index()

def get_summary(t):
    s = nasdaq(f"quote/{t}/summary?assetclass=stocks").get("data", {}).get("summaryData", {}) or {}
    g = lambda k: (s.get(k) or {}).get("value")
    return {"mktcap": g("MarketCap"), "target": g("OneYrTarget"),
            "sector": g("Sector"), "industry": g("Industry")}

def rsi(close, n=14):
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + gain / loss)

# ---------- 基本面（SEC EDGAR） ----------
_tickers = None
_CIK_OVERRIDE = {"XOM": "0000034088"}  # company_tickers.json 中 XOM 被映射到子公司 ExxonMobil Holdings，母公司埃克森美孚为 34088
def cik_of(t):
    if t in _CIK_OVERRIDE:
        return _CIK_OVERRIDE[t]
    global _tickers
    if _tickers is None:
        d = json.loads(fetch("https://www.sec.gov/files/company_tickers.json", ua=SEC_UA))
        _tickers = {r["ticker"]: str(r["cik_str"]).zfill(10) for r in d.values()}
    return _tickers[t]

def q_series(facts, tags):
    """取季度口径（约3个月时长）序列，返回按期末日排序的 Series。
    多个候选标签都可用时，选最新期末日的那条，避免公司改用新标签后
    仍取到已停用的旧标签（如 NVDA 2020 年后停用 RevenueFromContract...）。"""
    best = None
    for tag in tags:
        node = facts.get("us-gaap", {}).get(tag)
        if not node: continue
        units = node.get("units", {})
        arr = units.get("USD") or units.get("USD/shares") or []
        rows = []
        for e in arr:
            if "start" in e and "end" in e:
                days = (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days
                if 80 <= days <= 100:
                    rows.append((e["end"], e["val"]))
        if len(rows) >= 4:
            s = pd.Series(dict(rows)).sort_index()
            s.index = pd.to_datetime(s.index)
            if best is None or s.index[-1] > best.index[-1]:
                best = s
    return best

def instant(facts, tags):
    for tag in tags:
        node = facts.get("us-gaap", {}).get(tag)
        if not node: continue
        arr = node.get("units", {}).get("USD", [])
        if arr:
            e = max(arr, key=lambda x: x["end"])
            return e["val"], e["end"]
    return None, None

def fundamentals(t, price):
    facts = json.loads(fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_of(t)}.json", ua=SEC_UA))["facts"]
    rev = q_series(facts, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"])
    gp = q_series(facts, ["GrossProfit"])
    ni = q_series(facts, ["NetIncomeLoss"])
    eps = q_series(facts, ["EarningsPerShareDiluted"])
    ocf = q_series(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    capex = q_series(facts, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])
    eq, eq_date = instant(facts, ["StockholdersEquity"])
    res = {}
    if rev is not None and len(rev) >= 8:
        ttm, prev = rev.iloc[-4:].sum(), rev.iloc[-8:-4].sum()
        res["rev_ttm"] = ttm
        res["rev_growth"] = (ttm / prev - 1) * 100
        res["rev_period"] = str(rev.index[-1].date())
    if gp is not None and rev is not None:
        res["gross_margin"] = gp.iloc[-4:].sum() / rev.iloc[-4:].sum() * 100
    if ni is not None:
        res["ni_ttm"] = ni.iloc[-4:].sum()
        if eq: res["roe"] = res["ni_ttm"] / eq * 100
    if eps is not None and len(eps) >= 4:
        e = eps.iloc[-4:].sum()
        res["eps_ttm"] = e
        if e > 0: res["pe"] = price / e
    if ocf is not None and capex is not None:
        res["fcf_ttm"] = ocf.iloc[-4:].sum() - capex.iloc[-4:].sum()
    return res

# ---------- 宏观（FRED, 无需key的csv） ----------
def fred_context():
    try:
        txt = fetch("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10")
        lines = [l for l in txt.strip().splitlines()[1:] if l.split(",")[1] not in (".", "")]
        last_date, last_val = lines[-1].split(",")
        old_val = float(lines[-66].split(",")[1]) if len(lines) > 66 else None
        chg = f"，近3个月{float(last_val)-old_val:+.2f}个百分点" if old_val else ""
        return f"美国10年期国债收益率 {last_val}%（{last_date}{chg}）"
    except Exception:
        return "宏观背景暂不可用"

# ---------- 打分 ----------
def score(t):
    df = get_history(t)
    c, v = df["close"], df["volume"]
    last = c.iloc[-1]
    ma50, ma200 = c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1]
    mom3 = (last / c.iloc[-63] - 1) * 100
    mom6 = (last / c.iloc[-126] - 1) * 100
    r = rsi(c).iloc[-1]
    vol20 = c.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100
    dev50 = (last / ma50 - 1) * 100
    vol_ratio = v.tail(20).mean() / v.tail(60).mean()
    up = df.tail(60)
    up_share = up.loc[up["close"] >= up["open"], "volume"].sum() / up["volume"].sum() * 100
    sm = get_summary(t)

    F = [("趋势·价在200日线上方", f"现价{last:.2f}/线{ma200:.2f}", 1 if last > ma200 else -1),
         ("趋势·50日线在200日线上方", f"{ma50:.2f}/{ma200:.2f}", 1 if ma50 > ma200 else -1),
         ("动量·3个月", f"{mom3:+.1f}%", 1 if mom3 > 10 else (-1 if mom3 < -5 else 0)),
         ("动量·6个月", f"{mom6:+.1f}%", 1 if mom6 > 15 else (-1 if mom6 < -5 else 0)),
         ("量能·近20日量/60日量", f"{vol_ratio:.2f}倍", 1 if vol_ratio > 1.2 else (-0.5 if vol_ratio < 0.8 else 0)),
         ("量能·上涨日量能占比", f"{up_share:.0f}%", 1 if up_share > 55 else (-1 if up_share < 45 else 0)),
         ("位置·RSI(14)", f"{r:.0f}", -1 if r > 70 else (1 if r < 30 else 0)),
         ("位置·距50日线乖离", f"{dev50:+.1f}%", -1 if dev50 > 10 else (0.5 if abs(dev50) <= 3 else 0)),
         ("风险·20日波动率(年化)", f"{vol20:.0f}%", -0.5 if vol20 > 60 else 0)]

    print(f"\n===== {t} 因子打分卡 v2（行情截至 {df.index[-1].date()}）=====")
    if sm.get("sector"): print(f"行业 {sm['sector']}/{sm['industry']}；市值 {sm['mktcap']}；分析师目标价 {sm['target']}")
    print("-- 行情/资金因子 --")
    for n, val, s in F: print(f"  {n:<22} {val:<22} {s:+.1f}")
    tech_total = sum(s for _, _, s in F)

    print("-- 基本面因子（SEC EDGAR 官方财报）--")
    fb = fundamentals(t, last)
    bf = []
    if "rev_growth" in fb:
        g = fb["rev_growth"]
        bf.append(("营收增速(TTM同比)", f"{g:+.1f}%", 1 if g > 20 else (0.5 if g > 5 else (-1 if g < 0 else 0))))
    if "gross_margin" in fb:
        m = fb["gross_margin"]
        bf.append(("毛利率(TTM)", f"{m:.1f}%", 1 if m >= 40 else (0.5 if m >= 25 else (-1 if m < 15 else 0))))
    if "roe" in fb:
        m = fb["roe"]
        bf.append(("ROE(TTM)", f"{m:.1f}%", 1 if m > 15 else (0.5 if m > 5 else (-1 if m < 0 else 0))))
    if "fcf_ttm" in fb:
        bf.append(("自由现金流(TTM)", f"{fb['fcf_ttm']/1e9:+.2f}十亿美元", 0.5 if fb["fcf_ttm"] > 0 else -0.5))
    if bf:
        for n, val, s in bf: print(f"  {n:<22} {val:<22} {s:+.1f}")
    if "pe" in fb: print(f"  市盈率PE(TTM)          {fb['pe']:.1f}倍（参考，结合增速看，不单独打分）")
    if "rev_period" in fb: print(f"  （财报最新季度截至 {fb['rev_period']}，季报有滞后）")
    fund_total = sum(s for _, _, s in bf)
    print(f"  行情分 {tech_total:+.1f} ｜ 基本面分 {fund_total:+.1f} ｜ 合计 {tech_total + fund_total:+.1f}")

    notes = []
    if last > ma200 and ma50 > ma200: notes.append("大趋势向上")
    if dev50 > 10: notes.append("离50日线过远，追高风险大")
    elif abs(dev50) <= 3 and last > ma200: notes.append("回踩50日线附近且趋势未破，常见观察入场位")
    if r > 70: notes.append("RSI过热")
    if last < ma200: notes.append("在200日线下方，中长线趋势未确认")
    if "rev_growth" in fb and fb["rev_growth"] > 20: notes.append("营收高增长，基本面有支撑")
    if "rev_growth" in fb and fb["rev_growth"] < 0: notes.append("营收下滑，基本面拖后腿")
    print("  入场观察: " + ("；".join(notes) if notes else "信号中性，观望"))

if __name__ == "__main__":
    print("宏观背景: " + fred_context())
    for t in (sys.argv[1:] or ["MRVL"]):
        try: score(t.upper())
        except Exception as e: print(f"{t}: 失败（{type(e).__name__}: {e}）")
