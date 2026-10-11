"""
时点筛选练习：假设站在 2026-01-01，用 v3 逻辑（红线封顶 + 四本账）筛可买入候选。

严格时点纪律：
- 行情只用到 2025-12-31（含）收盘为止，之后的价格一律截断丢弃；
- 财报只用 filed（向 SEC 申报日）<= 2025-12-31 且期间结束日 <= 2025-12-31 的条目；
  同一期间有多份（修订）时，取截断日之前最后申报的那份，即当时市场能看到的版本；
- 市值用当时股价 × 当时股本（最近一期已申报）计算，不用今天的市值；
- 不使用分析师目标价、新闻、预期修正（当时口径也拿不到可靠免费源，v3 本就标缺失）。

数据：Nasdaq 公开行情、SEC EDGAR 官方财报。研究练习，不构成投资建议。
使用：python screen_asof_2026_01_01.py [TICKER ...]
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

from factor_scorecard_v2 import fetch, cik_of, get_history, rsi, SEC_UA
from factor_framework_v3 import interp, weighted_book, technicals, classify, series_growth, quarter_yoy, clamp

CUTOFF = pd.Timestamp("2025-12-31")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pit_cache")
os.makedirs(CACHE, exist_ok=True)

UNIVERSE = ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AVGO", "AMD", "MU", "MRVL",
            "PLTR", "CRM", "NOW", "ORCL", "NFLX", "TSLA", "COST", "WMT", "HD", "UNH",
            "LLY", "JNJ", "JPM", "V", "MA", "XOM", "CVX", "CAT", "GE", "ADBE"]


# ---------------- point-in-time 财报 ----------------

def load_facts(t):
    path = os.path.join(CACHE, f"{t}_facts.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)["facts"]
    raw = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_of(t)}.json", ua=SEC_UA)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return json.loads(raw)["facts"]


def pit_entries(facts, taxonomy, tag, units_pref):
    node = facts.get(taxonomy, {}).get(tag)
    if not node:
        return []
    units = node.get("units", {})
    arr = []
    for u in units_pref:
        if u in units:
            arr = units[u]
            break
    out = []
    for e in arr:
        try:
            end = pd.Timestamp(e["end"])
            filed = pd.Timestamp(e["filed"])
        except Exception:
            continue
        if end <= CUTOFF and filed <= CUTOFF:
            out.append(e)
    return out


def pit_q_series(facts, tags, taxonomy="us-gaap"):
    """季度序列（约3个月时长）。公司会换标签（如 Revenues 换成
    RevenueFromContract...），单标签序列会有缺口、把跨度超一年的4个点误当TTM；
    因此合并候选标签：同一期末多版本取截断日前最后申报的一份。"""
    rows = {}
    for tag in tags:
        for e in pit_entries(facts, taxonomy, tag, ["USD", "USD/shares", "shares"]):
            if "start" not in e:
                continue
            days = (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days
            if 80 <= days <= 100:
                end = pd.Timestamp(e["end"])
                filed = pd.Timestamp(e["filed"])
                if end not in rows or filed > rows[end][0]:
                    rows[end] = (filed, e["val"])
    if len(rows) < 4:
        return None
    return pd.Series({k: v[1] for k, v in rows.items()}).sort_index()


def pit_instant(facts, tags, taxonomy="us-gaap", units_pref=None):
    units_pref = units_pref or ["USD", "shares"]
    for tag in tags:
        cand = pit_entries(facts, taxonomy, tag, units_pref)
        if cand:
            e = max(cand, key=lambda x: (x["end"], x["filed"]))
            return e["val"], e["end"]
    return None, None


def pit_shares(facts):
    """股本：dei 的 EntityCommonStockSharesOutstanding 有些公司很早就停报
    （如 MA、V 只剩 2010 年前后的值），直接用会把市值算小数倍。因此：
    即时股本只在日期够新（截断日前 400 天内）时采用，否则退到最新一季的
    稀释加权股数（数量级正确，口径略粗，已在结果中注明来源）。"""
    cand = []
    for taxonomy, tag in [("dei", "EntityCommonStockSharesOutstanding"),
                          ("us-gaap", "CommonStockSharesOutstanding")]:
        c = pit_entries(facts, taxonomy, tag, ["shares"])
        if c:
            latest = max(pd.Timestamp(e["end"]) for e in c)
            if not cand or latest > max(pd.Timestamp(e["end"]) for e in cand):
                cand = c
    instant_val, instant_date = None, None
    if cand:
        latest_end = max(pd.Timestamp(e["end"]) for e in cand)
        same = [e for e in cand if pd.Timestamp(e["end"]) == latest_end]
        by_accn = {}
        for e in same:
            k = e.get("accn", "")
            if k not in by_accn or pd.Timestamp(e["filed"]) > pd.Timestamp(by_accn[k]["filed"]):
                by_accn[k] = e
        vals = list(by_accn.values())
        if len(vals) == 1:
            instant_val = float(vals[0]["val"])
        else:
            vs = sorted(float(v["val"]) for v in vals)
            # 多类别合计；数值接近时多半是重复披露，取最大值防重复计数
            instant_val = vs[-1] if vs[-1] and vs[0] / vs[-1] > 0.9 else float(sum(vs))
        instant_date = latest_end
    if instant_val and instant_date >= CUTOFF - pd.Timedelta(days=400):
        return instant_val, str(instant_date.date())
    s = pit_q_series(facts, ["WeightedAverageNumberOfDilutedSharesOutstanding"])
    if s is not None and len(s):
        return float(s.iloc[-1]), str(s.index[-1].date()) + "（稀释加权近似）"
    if instant_val:
        return instant_val, str(instant_date.date())
    return None, None


def pit_fundamentals(t, price):
    facts = load_facts(t)
    rev = pit_q_series(facts, ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"])
    gp = pit_q_series(facts, ["GrossProfit"])
    ni = pit_q_series(facts, ["NetIncomeLoss"])
    eps = pit_q_series(facts, ["EarningsPerShareDiluted"])
    ocf = pit_q_series(facts, ["NetCashProvidedByUsedInOperatingActivities"])
    capex = pit_q_series(facts, ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"])

    out = {}
    if rev is not None and len(rev) >= 4:
        out["rev_ttm"] = float(rev.iloc[-4:].sum())
        out["rev_period"] = str(rev.index[-1].date())
    out["rev_growth"] = series_growth(rev)
    q_yoy, q_yoy_prev = quarter_yoy(rev)
    out["rev_q_yoy"] = q_yoy
    out["rev_accel_pp"] = None if q_yoy is None or q_yoy_prev is None else q_yoy - q_yoy_prev
    if eps is not None and len(eps) >= 8:
        eps_cur = float(eps.iloc[-4:].sum())
        eps_prev = float(eps.iloc[-8:-4].sum())
        out["eps_growth"] = (eps_cur / eps_prev - 1) * 100 if eps_cur > 0 and eps_prev > 0 else None
    else:
        out["eps_growth"] = None
    # 毛利必须与营收覆盖同一组季度，否则（如 AMZN 的 GrossProfit 标签残缺）
    # 会算出 0.66% 这种假毛利率，宁可记缺失。
    if (gp is not None and rev is not None and len(gp) >= 4 and len(rev) >= 4
            and list(gp.index[-4:]) == list(rev.index[-4:])):
        out["gross_margin"] = float(gp.iloc[-4:].sum() / rev.iloc[-4:].sum() * 100)
        if len(gp) >= 8 and len(rev) >= 8 and list(gp.index[-8:-4]) == list(rev.index[-8:-4]):
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

    eq, _ = pit_instant(facts, ["StockholdersEquity"])
    if "ni_ttm" in out and eq:
        out["roe"] = out["ni_ttm"] / eq * 100
    cash, _ = pit_instant(facts, ["CashAndCashEquivalentsAtCarryingValue"])
    debt_cur, _ = pit_instant(facts, ["DebtCurrent", "LongTermDebtCurrent"])
    debt_noncur, _ = pit_instant(facts, ["LongTermDebtNoncurrent", "LongTermDebt"])
    ca, _ = pit_instant(facts, ["AssetsCurrent"])
    cl, _ = pit_instant(facts, ["LiabilitiesCurrent"])
    if cash is not None:
        out["cash"] = float(cash)
    debt_parts = [x for x in [debt_cur, debt_noncur] if x is not None]
    if debt_parts:
        out["debt"] = float(sum(debt_parts))
    if ca is not None and cl:
        out["current_ratio"] = float(ca / cl)
    if "debt" in out:
        out["net_debt"] = out["debt"] - (out.get("cash") or 0.0)
        if "fcf_ttm" in out and out["fcf_ttm"] > 0:
            out["net_debt_fcf"] = out["net_debt"] / out["fcf_ttm"]

    shares, shares_date = pit_shares(facts)
    if shares:
        out["shares"] = shares
        out["shares_date"] = shares_date
        out["mktcap_asof"] = shares * price
    if out.get("mktcap_asof") and out.get("rev_ttm"):
        out["ps"] = out["mktcap_asof"] / out["rev_ttm"]
    if out.get("mktcap_asof") and out.get("fcf_ttm") is not None:
        out["fcf_yield"] = out["fcf_ttm"] / out["mktcap_asof"] * 100
    if out.get("pe") and out.get("rev_growth") and out["rev_growth"] > 0:
        out["peg_rev_approx"] = out["pe"] / out["rev_growth"]

    # ---- 数据可信度检查：宁可作废，不拿错数打分 ----
    flags = []
    if rev is not None and len(rev) >= 4:
        if rev.index[-1] < CUTOFF - pd.Timedelta(days=180):
            flags.append(f"营收序列过期（最新只到 {str(rev.index[-1].date())}），基本面账不可靠")
            out["fund_unreliable"] = True
        elif (rev.index[-1] - rev.index[-4]).days > 380:
            flags.append("营收季度序列缺口过大，营收相关因子作废")
            for k in ["rev_growth", "rev_q_yoy", "rev_accel_pp", "gross_margin",
                      "gross_margin_change_pp", "fcf_margin", "ps", "peg_rev_approx"]:
                out.pop(k, None)
    # 拆股口径错位嫌疑：Nasdaq 历史价是拆股回溯价、财报 EPS/股本是拆股前口径，
    # PE/PS 会被算小数倍。特征：PE 低得离谱，或高毛利高成长公司 PS 低得离谱。
    pe_, ps_, gm_ = out.get("pe"), out.get("ps"), out.get("gross_margin")
    if (pe_ and pe_ < 5 and ps_ and ps_ < 2) or (ps_ and ps_ < 3 and gm_ and gm_ > 60):
        flags.append("疑似拆股口径错位，估值账作废待人工核对")
        out["split_suspect"] = True
    out["data_flags"] = flags
    return out


# ---------------- 四本账 + 红线（与 v3 同口径） ----------------

def analyze_asof(t, sector_hint=""):
    df_all = get_history(t)
    df = df_all[df_all.index <= CUTOFF]
    if len(df) < 260:
        raise ValueError(f"历史不足（{len(df)}行）")
    tech = technicals(df)
    f = pit_fundamentals(t, tech["price"])
    archetype, weights = classify({"sector": sector_hint, "industry": ""})

    quality_items = [
        ("毛利率", None if f.get("gross_margin") is None else f"{f['gross_margin']:.1f}%", interp(f.get("gross_margin"), [(5, 5), (15, 30), (25, 50), (40, 75), (60, 95)]), 25, ""),
        ("ROE", None if f.get("roe") is None else f"{f['roe']:.1f}%", interp(f.get("roe"), [(-10, 0), (0, 25), (5, 45), (15, 75), (25, 95)]), 25, ""),
        ("自由现金流率", None if f.get("fcf_margin") is None else f"{f['fcf_margin']:.1f}%", interp(f.get("fcf_margin"), [(-10, 0), (0, 30), (5, 55), (10, 75), (20, 95)]), 25, ""),
        ("现金流/净利润", None if f.get("fcf_conversion") is None else f"{f['fcf_conversion']:.2f}x", interp(f.get("fcf_conversion"), [(-0.5, 0), (0, 30), (0.5, 60), (1.0, 85), (1.5, 95)]), 15, ""),
        ("净负债/自由现金流", None if f.get("net_debt_fcf") is None else f"{f['net_debt_fcf']:.1f}x", interp(f.get("net_debt_fcf"), [(-1, 100), (0, 95), (1, 85), (3, 60), (5, 30), (8, 5)]), 10, ""),
    ]
    growth_items = [
        ("营收增速TTM", None if f.get("rev_growth") is None else f"{f['rev_growth']:+.1f}%", interp(f.get("rev_growth"), [(-15, 0), (-5, 15), (0, 35), (5, 50), (10, 65), (20, 82), (35, 95), (60, 100)]), 40, ""),
        ("营收增速加速度", None if f.get("rev_accel_pp") is None else f"{f['rev_accel_pp']:+.1f}pp", interp(f.get("rev_accel_pp"), [(-20, 5), (-10, 25), (-3, 45), (0, 55), (5, 70), (15, 90)]), 25, ""),
        ("EPS增速TTM", None if f.get("eps_growth") is None else f"{f['eps_growth']:+.1f}%", interp(f.get("eps_growth"), [(-30, 0), (-10, 20), (0, 40), (10, 60), (25, 80), (50, 95)]), 25, ""),
        ("毛利率变化", None if f.get("gross_margin_change_pp") is None else f"{f['gross_margin_change_pp']:+.1f}pp", interp(f.get("gross_margin_change_pp"), [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 95)]), 10, ""),
    ]
    valuation_items = [
        ("PE(TTM)", None if f.get("pe") is None else f"{f['pe']:.1f}x", interp(f.get("pe"), [(8, 98), (12, 90), (20, 72), (30, 52), (50, 28), (80, 10), (120, 3)]), 35, ""),
        ("PEG近似(PE/营收增速)", None if f.get("peg_rev_approx") is None else f"{f['peg_rev_approx']:.2f}", interp(f.get("peg_rev_approx"), [(0.5, 95), (1.0, 78), (1.5, 58), (2.5, 32), (4.0, 12)]), 25, ""),
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
    location_score = interp(tech["dev50"], [(-15, 25), (-8, 45), (-3, 75), (0, 85), (3, 80), (8, 55), (15, 25), (25, 5)])
    rsi_score = interp(tech["rsi"], [(20, 35), (30, 55), (45, 75), (60, 70), (70, 45), (80, 18), (90, 5)])
    risk_score = np.mean([
        interp(tech["vol20"], [(20, 90), (35, 70), (50, 50), (70, 25), (100, 5)]),
        interp(tech["drawdown252"], [(-60, 20), (-40, 35), (-25, 50), (-10, 70), (0, 85)]),
    ])
    timing_items = [
        ("趋势结构", f"价/MA200 {tech['price_vs_ma200']:+.1f}%，MA50/MA200 {tech['ma50_vs_ma200']:+.1f}%", float(trend_score), 35, ""),
        ("中期动量", f"3月 {tech['mom3']:+.1f}%，6月 {tech['mom6']:+.1f}%", float(momentum_score), 20, ""),
        ("量能确认", f"20/60量比 {tech['vol_ratio']:.2f}，上涨日量占比 {tech['up_share']:.0f}%", float(volume_score), 15, ""),
        ("入场位置", f"距MA50 {tech['dev50']:+.1f}%，RSI {tech['rsi']:.0f}", float(np.mean([location_score, rsi_score])), 20, ""),
        ("波动/回撤风险", f"20日年化波动 {tech['vol20']:.0f}%，距252日高点 {tech['drawdown252']:.1f}%", float(risk_score), 10, ""),
    ]

    if f.get("split_suspect"):
        valuation_items = [(n, "作废（口径存疑）", None, w, note) for n, _r, _s, w, note in valuation_items]
    if f.get("fund_unreliable"):
        quality_items = [(n, "作废（数据过期）", None, w, note) for n, _r, _s, w, note in quality_items]
        growth_items = [(n, "作废（数据过期）", None, w, note) for n, _r, _s, w, note in growth_items]
        valuation_items = [(n, "作废（数据过期）", None, w, note) for n, _r, _s, w, note in valuation_items]

    quality, quality_cov, _ = weighted_book(quality_items)
    growth, growth_cov, _ = weighted_book(growth_items)
    valuation, valuation_cov, _ = weighted_book(valuation_items)
    timing, timing_cov, _ = weighted_book(timing_items)

    redlines, caps = [], []
    if tech["price_vs_ma200"] < 0 and tech["ma50_vs_ma200"] < 0 and tech["mom6"] < -10:
        redlines.append("趋势破位：价在200日线下、50日线在200日线下、6个月动量为负")
        caps.append("排除/只观察")
    if f.get("fcf_ttm") is not None and f["fcf_ttm"] < 0 and (f.get("rev_growth") or 0) < 0:
        redlines.append("现金流为负且营收下滑")
        caps.append("排除/只观察")
    if (f.get("rev_growth") is not None and f["rev_growth"] < -5) and (f.get("eps_growth") is not None and f["eps_growth"] < -20):
        redlines.append("基本面恶化：营收与EPS同时明显下滑")
        caps.append("排除/只观察")
    if f.get("current_ratio") is not None and f["current_ratio"] < 0.8:
        redlines.append(f"流动性偏紧：流动比率 {f['current_ratio']:.2f}")
        caps.append("只观察")
    if f.get("pe") and f["pe"] > 80 and ((f.get("rev_growth") or 0) < 15 or (f.get("rev_accel_pp") or 0) < -10):
        redlines.append("高估值与增长不匹配：PE>80，但营收增速不足或明显减速")
        caps.append("好公司也要等价格")
    if tech["rsi"] > 80 and tech["dev50"] > 15:
        redlines.append("短线过热：RSI>80 且距50日线>15%")
        caps.append("不追高，等回踩")

    books = {"公司质量": quality, "成长与预期": growth, "估值": valuation, "入场时机": timing}
    covs = {"公司质量": quality_cov, "成长与预期": growth_cov, "估值": valuation_cov, "入场时机": timing_cov}
    excluded = any("排除" in c for c in caps)
    pass_all = (not excluded and quality is not None and growth is not None and valuation is not None and timing is not None
                and quality >= 60 and growth >= 55 and valuation >= 45 and timing >= 55)

    # 排序分：只用于候选内部排序（分类权重先验去掉未覆盖的催化5%后归一），不是“可买总分”
    wmap = {"公司质量": weights.get("公司质量", 25), "成长与预期": weights.get("成长与预期", 30),
            "估值": weights.get("估值", 25), "入场时机": weights.get("入场时机", 15)}
    wsum = sum(wmap.values())
    rank_score = None
    if all(v is not None for v in books.values()):
        rank_score = round(sum(books[k] * wmap[k] for k in books) / wsum, 1)

    all_items = []
    book_items = {"公司质量": quality_items, "成长与预期": growth_items, "估值": valuation_items, "入场时机": timing_items}
    for book, items in book_items.items():
        for name, raw, s, w, note in items:
            if s is not None:
                all_items.append((s, book, name, raw))
    supports = [f"{b}·{n} {r}（{s:.0f}）" for s, b, n, r in sorted(all_items, reverse=True)[:3]]
    risks = [f"{b}·{n} {r}（{s:.0f}）" for s, b, n, r in sorted(all_items)[:3]]

    return {
        "ticker": t, "archetype": archetype, "asof_price": tech["price"], "asof_date": tech["date"],
        "rev_period": f.get("rev_period"), "books": books, "coverage": covs, "rank_score": rank_score,
        "redlines": redlines, "caps": caps, "excluded": excluded, "pass_all": pass_all,
        "supports": supports, "risks": risks, "data_notes": f.get("data_flags", []),
        "tech": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tech.items()},
        "fund": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f.items()},
    }


SECTORS = {"AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "GOOGL": "Technology",
           "META": "Technology", "AMZN": "Technology", "AVGO": "Technology", "AMD": "Technology",
           "MU": "Technology", "MRVL": "Technology", "PLTR": "Technology", "CRM": "Technology",
           "NOW": "Technology", "ORCL": "Technology", "ADBE": "Technology", "NFLX": "Technology",
           "TSLA": "Consumer Cyclical", "COST": "Consumer Defensive", "WMT": "Consumer Defensive",
           "HD": "Consumer Cyclical", "UNH": "Healthcare", "LLY": "Healthcare", "JNJ": "Healthcare",
           "JPM": "Financial Services", "V": "Financial Services", "MA": "Financial Services",
           "XOM": "Energy", "CVX": "Energy", "CAT": "Industrials", "GE": "Industrials"}

if __name__ == "__main__":
    tickers = [x.upper() for x in sys.argv[1:]] or UNIVERSE
    results = []
    for t in tickers:
        try:
            r = analyze_asof(t, SECTORS.get(t, ""))
            results.append(r)
            b = r["books"]
            print(f"{t:6s} 质量{b['公司质量']} 成长{b['成长与预期']} 估值{b['估值']} 时机{b['入场时机']} "
                  f"排序分{r['rank_score']} 候选={'是' if r['pass_all'] else '否'} 红线={len(r['redlines'])} 价@2025-12-31={r['asof_price']:.2f} 财报至{r['rev_period']}", flush=True)
        except Exception as e:
            print(f"{t:6s} 失败（{type(e).__name__}: {e}）", flush=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "screen_asof_2026_01_01_results.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
    passed = sorted([r for r in results if r["pass_all"]], key=lambda r: -(r["rank_score"] or 0))
    print("\n=== 2026-01-01 时点候选（四本账同时过线、无红线）===")
    for r in passed:
        print(f"- {r['ticker']}（{r['archetype']}）排序分 {r['rank_score']}｜质量{r['books']['公司质量']} 成长{r['books']['成长与预期']} 估值{r['books']['估值']} 时机{r['books']['入场时机']}")
