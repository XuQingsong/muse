"""
因子框架 v3：红线封顶 + 四本账 + 分类权重（暂定先验）+ 证据链

与 v2 的区别：
- 不再把所有因子等权加成一个总分；
- 公司质量、成长、估值、入场时机分别给 0-100 分；
- 红线/组合风险会直接封顶结论，好分数不能抵消致命问题；
- 权重只用于说明研究优先级，不输出一个“可买总分”；
- 分析师预期修正与新闻催化目前没有可靠免费数据源，明确标缺失，不假装覆盖。

数据：Nasdaq 公开行情、SEC EDGAR 官方财报。财报按已披露的最新期间使用，
适合当前截面研究；历史回测若要验证基本面因子，必须另做 point-in-time 数据集，
不能用今天回填的历史财报冒充当时可见数据。

使用：python factor_framework_v3.py MRVL VST
不构成投资建议。
"""
import json
import math
import sys

import numpy as np
import pandas as pd

from factor_scorecard_v2 import fetch, cik_of, q_series, instant, get_history, get_summary, rsi


def clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(x)))


def interp(x, points):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return float(np.interp(x, xs, ys))


def weighted_book(items):
    """items: (name, raw_text, score_or_None, weight, note). 返回分、覆盖率、明细。"""
    usable = [(n, raw, s, w, note) for n, raw, s, w, note in items if s is not None]
    total_w = sum(w for *_x, w, _n in [(a, b, c, d, e) for a, b, c, d, e in items]) if False else sum(x[3] for x in items)
    used_w = sum(x[3] for x in usable)
    if not usable or used_w == 0:
        return None, 0.0, items
    score = sum(s * w for _n, _r, s, w, _note in usable) / used_w
    return round(score, 1), round(used_w / total_w * 100, 0) if total_w else 0.0, items


def parse_money(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("$", "").replace(",", "").strip()
    mult = 1.0
    if s.upper().endswith("B"):
        mult, s = 1e9, s[:-1]
    elif s.upper().endswith("M"):
        mult, s = 1e6, s[:-1]
    elif s.upper().endswith("T"):
        mult, s = 1e12, s[:-1]
    try:
        return float(s) * mult
    except Exception:
        return None


def series_growth(s, periods=4):
    if s is None or len(s) < periods * 2:
        return None
    cur = s.iloc[-periods:].sum()
    prev = s.iloc[-periods * 2:-periods].sum()
    if prev == 0:
        return None
    return (cur / prev - 1) * 100


def quarter_yoy(s):
    if s is None or len(s) < 8:
        return None, None
    vals = []
    for back in (0, 1):
        cur = s.iloc[-1 - back]
        prior = s.iloc[-5 - back] if len(s) >= 5 + back else None
        vals.append((cur / prior - 1) * 100 if prior not in (None, 0) else None)
    # vals[0] 最新季同比，vals[1] 上一季同比
    return vals[0], vals[1]


def extended_fundamentals(t, price):
    facts = json.loads(fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_of(t)}.json"))["facts"]
    # factor_scorecard_v2.fetch 默认带浏览器 UA；SEC 对 UA 不敏感于本研究脚本的低频调用。
    rev = q_series(facts, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"])
    gp = q_series(facts, ["GrossProfit"])
    ni = q_series(facts, ["NetIncomeLoss"])
    eps = q_series(facts, ["EarningsPerShareDiluted"])
    ocf = q_series(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    capex = q_series(facts, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])

    out = {}
    if rev is not None and len(rev) >= 4:
        out["rev_ttm"] = float(rev.iloc[-4:].sum())
        out["rev_period"] = str(rev.index[-1].date())
    out["rev_growth"] = series_growth(rev)
    q_yoy, q_yoy_prev = quarter_yoy(rev)
    out["rev_q_yoy"] = q_yoy
    out["rev_accel_pp"] = None if q_yoy is None or q_yoy_prev is None else q_yoy - q_yoy_prev
    # EPS 从亏损/微利基数跳到盈利（或反过来）时，百分比增速没有经济含义，直接记缺失，
    # 避免 -300% 这类数字把成长账打穿。PE 仍用当前 EPS 计算。
    if eps is not None and len(eps) >= 8:
        eps_cur = float(eps.iloc[-4:].sum())
        eps_prev = float(eps.iloc[-8:-4].sum())
        out["eps_growth"] = (eps_cur / eps_prev - 1) * 100 if eps_cur > 0 and eps_prev > 0 else None
    else:
        out["eps_growth"] = None
    if gp is not None and rev is not None and len(gp) >= 4 and len(rev) >= 4:
        out["gross_margin"] = float(gp.iloc[-4:].sum() / rev.iloc[-4:].sum() * 100)
        if len(gp) >= 8 and len(rev) >= 8:
            prior_gm = float(gp.iloc[-8:-4].sum() / rev.iloc[-8:-4].sum() * 100)
            out["gross_margin_change_pp"] = out["gross_margin"] - prior_gm
    if ni is not None and len(ni) >= 4:
        out["ni_ttm"] = float(ni.iloc[-4:].sum())
    if eps is not None and len(eps) >= 4:
        e = float(eps.iloc[-4:].sum())
        out["eps_ttm"] = e
        if e > 0:
            out["pe"] = price / e
    if ocf is not None and capex is not None and len(ocf) >= 4 and len(capex) >= 4:
        out["fcf_ttm"] = float(ocf.iloc[-4:].sum() - capex.iloc[-4:].sum())
    if "fcf_ttm" in out and "rev_ttm" in out and out["rev_ttm"]:
        out["fcf_margin"] = out["fcf_ttm"] / out["rev_ttm"] * 100
    if "fcf_ttm" in out and "ni_ttm" in out and out["ni_ttm"]:
        out["fcf_conversion"] = out["fcf_ttm"] / out["ni_ttm"]

    eq, _ = instant(facts, ["StockholdersEquity"])
    if "ni_ttm" in out and eq:
        out["roe"] = out["ni_ttm"] / eq * 100
    cash, _ = instant(facts, ["CashAndCashEquivalentsAtCarryingValue"])
    debt_cur, _ = instant(facts, ["DebtCurrent", "LongTermDebtCurrent"])
    debt_noncur, _ = instant(facts, ["LongTermDebtNoncurrent", "LongTermDebt"])
    liabilities, _ = instant(facts, ["Liabilities"])
    assets, _ = instant(facts, ["Assets"])
    ca, _ = instant(facts, ["AssetsCurrent"])
    cl, _ = instant(facts, ["LiabilitiesCurrent"])
    if cash is not None:
        out["cash"] = float(cash)
    debt_parts = [x for x in [debt_cur, debt_noncur] if x is not None]
    if debt_parts:
        out["debt"] = float(sum(debt_parts))
    if liabilities is not None and assets:
        out["liabilities_assets"] = float(liabilities / assets)
    if ca is not None and cl:
        out["current_ratio"] = float(ca / cl)
    if "debt" in out:
        out["net_debt"] = out["debt"] - (out.get("cash") or 0.0)
        if "fcf_ttm" in out and out["fcf_ttm"] > 0:
            out["net_debt_fcf"] = out["net_debt"] / out["fcf_ttm"]
    return out


def technicals(df):
    c, v = df["close"], df["volume"]
    last = float(c.iloc[-1])
    ma50 = float(c.rolling(50).mean().iloc[-1])
    ma200 = float(c.rolling(200).mean().iloc[-1])
    ma200_prev = float(c.iloc[:-20].rolling(200).mean().iloc[-1]) if len(c) >= 220 else ma200
    window = df.tail(60)
    up_share = float(window.loc[window["close"] >= window["open"], "volume"].sum() / window["volume"].sum() * 100)
    return {
        "price": last,
        "ma50": ma50,
        "ma200": ma200,
        "price_vs_ma200": (last / ma200 - 1) * 100,
        "ma50_vs_ma200": (ma50 / ma200 - 1) * 100,
        "ma200_slope20": (ma200 / ma200_prev - 1) * 100,
        "mom3": (last / float(c.iloc[-63]) - 1) * 100,
        "mom6": (last / float(c.iloc[-126]) - 1) * 100,
        "rsi": float(rsi(c).iloc[-1]),
        "dev50": (last / ma50 - 1) * 100,
        "vol_ratio": float(v.tail(20).mean() / v.tail(60).mean()),
        "up_share": up_share,
        "vol20": float(c.pct_change().rolling(20).std().iloc[-1] * math.sqrt(252) * 100),
        "drawdown252": (last / float(c.tail(252).max()) - 1) * 100,
        "date": str(df.index[-1].date()),
    }


def classify(summary):
    sector = (summary.get("sector") or "").lower()
    industry = (summary.get("industry") or "").lower()
    text = sector + " " + industry
    if "semiconductor" in text or "technology" in text:
        return "成长/周期科技", {"成长与预期": 35, "公司质量": 25, "估值": 20, "入场时机": 15, "催化/预期修正": 5}
    if "utilities" in text or "electric" in text or "energy" in text:
        return "资本密集/公用事业型", {"公司质量": 30, "估值": 30, "成长与预期": 20, "入场时机": 15, "催化/预期修正": 5}
    if "software" in text or "internet" in text or "communication" in text:
        return "平台/软件成长型", {"成长与预期": 35, "公司质量": 25, "估值": 25, "入场时机": 10, "催化/预期修正": 5}
    return "通用型（行业未知）", {"成长与预期": 30, "公司质量": 25, "估值": 25, "入场时机": 15, "催化/预期修正": 5}


def analyze(t):
    df = get_history(t)
    sm = get_summary(t)
    summary = {"sector": sm.get("sector"), "industry": sm.get("industry"), "mktcap": parse_money(sm.get("mktcap")), "target": sm.get("target")}
    tech = technicals(df)
    f = extended_fundamentals(t, tech["price"])
    archetype, weights = classify(summary)

    if summary["mktcap"] and f.get("rev_ttm"):
        f["ps"] = summary["mktcap"] / f["rev_ttm"]
    if summary["mktcap"] and f.get("fcf_ttm") is not None:
        f["fcf_yield"] = f["fcf_ttm"] / summary["mktcap"] * 100
    if f.get("pe") and f.get("rev_growth") and f["rev_growth"] > 0:
        # PEG 用营收增速近似（不是 EPS 预期增速），只作粗筛，必须在输出里注明。
        f["peg_rev_approx"] = f["pe"] / f["rev_growth"]

    quality_items = [
        ("毛利率", None if f.get("gross_margin") is None else f"{f['gross_margin']:.1f}%", interp(f.get("gross_margin"), [(5, 5), (15, 30), (25, 50), (40, 75), (60, 95)]), 25, "行业差异大，只作同账内参考"),
        ("ROE", None if f.get("roe") is None else f"{f['roe']:.1f}%", interp(f.get("roe"), [(-10, 0), (0, 25), (5, 45), (15, 75), (25, 95)]), 25, ""),
        ("自由现金流率", None if f.get("fcf_margin") is None else f"{f['fcf_margin']:.1f}%", interp(f.get("fcf_margin"), [(-10, 0), (0, 30), (5, 55), (10, 75), (20, 95)]), 25, ""),
        ("现金流/净利润", None if f.get("fcf_conversion") is None else f"{f['fcf_conversion']:.2f}x", interp(f.get("fcf_conversion"), [(-0.5, 0), (0, 30), (0.5, 60), (1.0, 85), (1.5, 95)]), 15, ""),
        ("净负债/自由现金流", None if f.get("net_debt_fcf") is None else f"{f['net_debt_fcf']:.1f}x", interp(f.get("net_debt_fcf"), [(-1, 100), (0, 95), (1, 85), (3, 60), (5, 30), (8, 5)]), 10, "FCF为负时不计算，另看红线"),
    ]
    growth_items = [
        ("营收增速TTM", None if f.get("rev_growth") is None else f"{f['rev_growth']:+.1f}%", interp(f.get("rev_growth"), [(-15, 0), (-5, 15), (0, 35), (5, 50), (10, 65), (20, 82), (35, 95), (60, 100)]), 40, ""),
        ("营收增速加速度", None if f.get("rev_accel_pp") is None else f"{f['rev_accel_pp']:+.1f}pp", interp(f.get("rev_accel_pp"), [(-20, 5), (-10, 25), (-3, 45), (0, 55), (5, 70), (15, 90)]), 25, "最新季同比减上一季同比"),
        ("EPS增速TTM", None if f.get("eps_growth") is None else f"{f['eps_growth']:+.1f}%", interp(f.get("eps_growth"), [(-30, 0), (-10, 20), (0, 40), (10, 60), (25, 80), (50, 95)]), 25, "EPS基数低时容易失真"),
        ("毛利率变化", None if f.get("gross_margin_change_pp") is None else f"{f['gross_margin_change_pp']:+.1f}pp", interp(f.get("gross_margin_change_pp"), [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 95)]), 10, ""),
    ]
    valuation_items = [
        ("PE(TTM)", None if f.get("pe") is None else f"{f['pe']:.1f}x", interp(f.get("pe"), [(8, 98), (12, 90), (20, 72), (30, 52), (50, 28), (80, 10), (120, 3)]), 35, "亏损公司无PE时记缺失并看PS/FCF"),
        ("PEG近似(PE/营收增速)", None if f.get("peg_rev_approx") is None else f"{f['peg_rev_approx']:.2f}", interp(f.get("peg_rev_approx"), [(0.5, 95), (1.0, 78), (1.5, 58), (2.5, 32), (4.0, 12)]), 25, "不是分析师EPS预期PEG"),
        ("市销率PS", None if f.get("ps") is None else f"{f['ps']:.1f}x", interp(f.get("ps"), [(0.5, 98), (1, 90), (3, 72), (6, 50), (10, 28), (20, 8)]), 20, ""),
        ("自由现金流收益率", None if f.get("fcf_yield") is None else f"{f['fcf_yield']:.1f}%", interp(f.get("fcf_yield"), [(-2, 0), (0, 12), (1, 32), (3, 58), (5, 78), (8, 95)]), 20, ""),
    ]

    trend_score = np.mean([
        interp(tech["price_vs_ma200"], [(-15, 5), (-5, 30), (0, 55), (5, 75), (15, 90)]),
        interp(tech["ma50_vs_ma200"], [(-10, 5), (-3, 30), (0, 55), (3, 75), (10, 90)]),
        interp(tech["ma200_slope20"], [(-2, 10), (-0.5, 35), (0, 55), (0.5, 75), (2, 90)]),
    ])
    momentum_score = np.mean([
        interp(tech["mom3"], [(-20, 5), (-10, 25), (0, 50), (10, 70), (25, 85), (50, 90)]),
        interp(tech["mom6"], [(-30, 5), (-15, 25), (0, 50), (15, 70), (40, 85), (80, 90)]),
    ])
    volume_score = np.mean([
        interp(tech["vol_ratio"], [(0.5, 25), (0.8, 45), (1.0, 60), (1.2, 75), (1.8, 85)]),
        interp(tech["up_share"], [(35, 10), (45, 35), (50, 55), (55, 70), (65, 90)]),
    ])
    # 时机账不是“涨得越猛越好”：位置过热要降分；RSI 只在趋势未破时作回踩/过热参考。
    location_score = interp(tech["dev50"], [(-15, 25), (-8, 45), (-3, 75), (0, 85), (3, 80), (8, 55), (15, 25), (25, 5)])
    rsi_score = interp(tech["rsi"], [(20, 35), (30, 55), (45, 75), (60, 70), (70, 45), (80, 18), (90, 5)])
    risk_score = np.mean([
        interp(tech["vol20"], [(20, 90), (35, 70), (50, 50), (70, 25), (100, 5)]),
        interp(tech["drawdown252"], [(-60, 20), (-40, 35), (-25, 50), (-10, 70), (0, 85)]),
    ])
    timing_items = [
        ("趋势结构", f"价/MA200 {tech['price_vs_ma200']:+.1f}%，MA50/MA200 {tech['ma50_vs_ma200']:+.1f}%", float(trend_score), 35, "只决定入场风险，不当收益预测"),
        ("中期动量", f"3月 {tech['mom3']:+.1f}%，6月 {tech['mom6']:+.1f}%", float(momentum_score), 20, "历史验证里追高动量并不占优"),
        ("量能确认", f"20/60量比 {tech['vol_ratio']:.2f}，上涨日量占比 {tech['up_share']:.0f}%", float(volume_score), 15, ""),
        ("入场位置", f"距MA50 {tech['dev50']:+.1f}%，RSI {tech['rsi']:.0f}", float(np.mean([location_score, rsi_score])), 20, "过热降分，不把RSI高简单当强势"),
        ("波动/回撤风险", f"20日年化波动 {tech['vol20']:.0f}%，距252日高点 {tech['drawdown252']:.1f}%", float(risk_score), 10, ""),
    ]

    quality, quality_cov, _ = weighted_book(quality_items)
    growth, growth_cov, _ = weighted_book(growth_items)
    valuation, valuation_cov, _ = weighted_book(valuation_items)
    timing, timing_cov, _ = weighted_book(timing_items)

    redlines = []
    caps = []
    if tech["price_vs_ma200"] < 0 and tech["ma50_vs_ma200"] < 0 and tech["mom6"] < -10:
        redlines.append("趋势破位：价在200日线下、50日线在200日线下、6个月动量为负")
        caps.append("封顶：排除/只观察")
    if f.get("fcf_ttm") is not None and f["fcf_ttm"] < 0 and (f.get("rev_growth") or 0) < 0:
        redlines.append("现金流为负且营收下滑：账面故事缺少现金支撑")
        caps.append("封顶：排除/只观察")
    if (f.get("rev_growth") is not None and f["rev_growth"] < -5) and (f.get("eps_growth") is not None and f["eps_growth"] < -20):
        redlines.append("基本面恶化：营收与EPS同时明显下滑")
        caps.append("封顶：排除/只观察")
    if f.get("current_ratio") is not None and f["current_ratio"] < 0.8:
        redlines.append(f"流动性偏紧：流动比率 {f['current_ratio']:.2f}")
        caps.append("封顶：只观察")
    if f.get("pe") and f["pe"] > 80 and ((f.get("rev_growth") or 0) < 15 or (f.get("rev_accel_pp") or 0) < -10):
        redlines.append("高估值与增长不匹配：PE>80，但营收增速不足或明显减速")
        caps.append("封顶：好公司也要等价格")
    if tech["rsi"] > 80 and tech["dev50"] > 15:
        redlines.append("短线过热：RSI>80 且距50日线>15%")
        caps.append("封顶：不追高，等回踩")

    interactions = []
    if (valuation is not None and valuation < 35) and (growth is not None and growth >= 65):
        interactions.append("高增长 + 低估值分：不是不能研究，而是要先确认增长减速/预期下修风险，不能被成长分抵消估值风险")
    if tech["price_vs_ma200"] > 0 and tech["rsi"] > 75:
        interactions.append("趋势向上但短线过热：公司逻辑与入场时机要分开，趋势分不能抵消追高风险")
    if (f.get("fcf_ttm") or 0) > 0 and (f.get("net_debt_fcf") or 0) > 5:
        interactions.append("有自由现金流但杠杆不低：质量账要同时看偿债，不要只看FCF为正")

    books = {
        "公司质量": (quality, quality_cov, quality_items),
        "成长与预期": (growth, growth_cov, growth_items),
        "估值": (valuation, valuation_cov, valuation_items),
        "入场时机": (timing, timing_cov, timing_items),
    }

    if any("排除" in c for c in caps):
        verdict = "排除/只观察（红线封顶）"
    elif any("不追高" in c for c in caps):
        verdict = "好公司/强势股也要等回踩（时机封顶）"
    elif quality and growth and quality >= 65 and growth >= 60 and valuation is not None and valuation < 40:
        verdict = "基本面可研究，但等价格"
    elif quality and growth and quality >= 65 and growth >= 60 and timing is not None and timing < 50:
        verdict = "基本面可研究，但等时机"
    elif quality and growth and valuation and timing and quality >= 60 and growth >= 55 and valuation >= 45 and timing >= 55:
        verdict = "进入研究候选（仍需催化/预期修正确认）"
    else:
        verdict = "观察：四本账没有同时过线"

    supports, risks = [], []
    all_items = []
    for book, (score, cov, items) in books.items():
        for name, raw, s, w, note in items:
            if s is not None:
                all_items.append((s, book, name, raw))
    for s, book, name, raw in sorted(all_items, reverse=True)[:3]:
        supports.append(f"{book}·{name} {raw}（{s:.0f}）")
    for s, book, name, raw in sorted(all_items)[:3]:
        risks.append(f"{book}·{name} {raw}（{s:.0f}）")

    missing = []
    if f.get("pe") is None:
        missing.append("PE缺失（可能亏损或EPS口径异常）")
    missing.append("分析师预期修正：无可靠免费源，未评分")
    missing.append("新闻/事件催化：无可靠结构化免费源，未评分")

    return {
        "ticker": t,
        "archetype": archetype,
        "weights_prior": weights,
        "summary": summary,
        "technical": tech,
        "fundamentals": f,
        "books": {k: {"score": v[0], "coverage_pct": v[1], "items": v[2]} for k, v in books.items()},
        "redlines": redlines,
        "caps": caps,
        "interactions": interactions,
        "verdict": verdict,
        "supports": supports,
        "risks": risks,
        "missing": missing,
    }


def fmt_items(items):
    lines = []
    for name, raw, s, w, note in items:
        ss = "缺失" if s is None else f"{s:.0f}"
        lines.append(f"    - {name}: {raw or '—'} → {ss}（账内权重{w}%）" + (f"；{note}" if note else ""))
    return lines


def print_report(r):
    sm, tech, f = r["summary"], r["technical"], r["fundamentals"]
    print(f"\n===== {r['ticker']} 因子框架 v3（行情 {tech['date']}；财报最新期间 {f.get('rev_period', '未知')}）=====")
    print(f"类型：{r['archetype']}；行业：{sm.get('sector') or '—'}/{sm.get('industry') or '—'}；现价 {tech['price']:.2f}")
    print("结论：" + r["verdict"])
    if r["redlines"]:
        print("红线/封顶：")
        for x in r["redlines"]:
            print(f"  - {x}")
    else:
        print("红线/封顶：未触发已定义红线（不等于无风险）")
    print("四本账（0-100；不是总分，不互相抵消）：")
    for book in ["公司质量", "成长与预期", "估值", "入场时机"]:
        b = r["books"][book]
        print(f"  {book}: {b['score']}（覆盖率 {b['coverage_pct']:.0f}%）")
        for line in fmt_items(b["items"]):
            print(line)
    print(f"分类权重先验（只说明看哪本账更重，不合成总分）：{r['weights_prior']}")
    print("主要支持：" + ("；".join(r["supports"]) if r["supports"] else "无"))
    print("主要反对：" + ("；".join(r["risks"]) if r["risks"] else "无"))
    if r["interactions"]:
        print("组合关系：")
        for x in r["interactions"]:
            print(f"  - {x}")
    print("缺失/局限：" + "；".join(r["missing"]))
    print("反转条件示例：估值账回到45以上、或营收加速转负、或价格跌破200日线，结论都要重算。")


if __name__ == "__main__":
    tickers = [x.upper() for x in sys.argv[1:]] or ["MRVL", "VST"]
    results = []
    for t in tickers:
        try:
            r = analyze(t)
            results.append(r)
            print_report(r)
        except Exception as e:
            print(f"\n{t}: 失败（{type(e).__name__}: {e}）")
    with open("factor_framework_v3_results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
