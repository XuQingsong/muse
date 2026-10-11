"""
纸面模拟：把 2026-01-01 时点没过线的股票，从 2026-01-01 起逐日用当时可见数据
重算 v3 四本账，按事先定死的规则模拟买卖，统计累计收益。

时点纪律（每个交易日 t）：
- 行情只用 t 日（含）之前的数据；财报只用 filed <= t 的版本（与筛选脚本同一 PIT 口径）；
- 信号在 t 日收盘后生成，t+1 日开盘成交，避免用收盘信号按收盘价成交的前视；
- 财报因子只在新申报到达时变化（状态机），估值随当日股价逐日更新。

规则（运行前写死）：
- 模型账户初始 100 万美元，只做多，最多同时持 5 只，单只目标仓位 = 当时总权益 20%；
- 买入：四本账同时过线（质量>=60、成长>=55、估值>=45、时机>=55）且无排除红线；
- 全卖：触发排除红线，或任一本账跌破退出线（质量<45、成长<40、估值<35、时机<40）；
- 过热红线（RSI>80 且距MA50>15%）：卖出一半；之后重新过线且仓位低于目标时买回；
- 成本：每笔成交额 0.1%；不计股息、税费、现金利息。
研究练习，不构成投资建议。
"""
import json
import math
import os
import sys

import numpy as np
import pandas as pd

import screen_asof_2026_01_01 as S
from factor_scorecard_v2 import get_history, rsi
from factor_framework_v3 import interp, weighted_book, classify

START = pd.Timestamp("2026-01-01")
INIT_CASH = 1_000_000.0
MAX_POS = 5
TARGET_W = 0.20
COST = 0.001
BOOK_MIN = {"公司质量": 60, "成长与预期": 55, "估值": 45, "入场时机": 55}
EXIT_MIN = {"公司质量": 45, "成长与预期": 40, "估值": 35, "入场时机": 40}

# 2026-01-01 未过线的 28 只（MU、GOOGL 为当时过线者，不在本模拟范围）
TICKERS = ["AAPL", "MSFT", "NVDA", "META", "AMZN", "AVGO", "AMD", "MRVL", "PLTR", "CRM",
           "NOW", "ORCL", "NFLX", "TSLA", "COST", "WMT", "HD", "UNH", "LLY", "JNJ",
           "JPM", "V", "MA", "XOM", "CVX", "CAT", "GE", "ADBE"]

_facts_memo = {}
_orig_load_facts = S.load_facts


def load_facts_memo(t):
    if t not in _facts_memo:
        _facts_memo[t] = _orig_load_facts(t)
    return _facts_memo[t]


S.load_facts = load_facts_memo

RELEVANT_TAGS = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                 "NetIncomeLoss", "EarningsPerShareDiluted", "NetCashProvidedByUsedInOperatingActivities",
                 "PaymentsToAcquirePropertyPlantAndEquipment", "GrossProfit"]


def fund_states(t):
    """返回 [(生效日, 因子水平dict)]：2025-12-31 初始状态 + 2026 年每次新申报后的状态。"""
    facts = load_facts_memo(t)
    dates = {pd.Timestamp("2025-12-31")}
    for tag in RELEVANT_TAGS:
        node = facts.get("us-gaap", {}).get(tag)
        if not node:
            continue
        for units in node.get("units", {}).values():
            for e in units:
                form = str(e.get("form", ""))
                if form.startswith(("10-Q", "10-K", "20-F", "6-K")) and "filed" in e:
                    d = pd.Timestamp(e["filed"])
                    if START <= d:
                        dates.add(d)
    states = []
    for d in sorted(dates):
        S.CUTOFF = d
        f = S.pit_fundamentals(t, 1.0)
        for k in ["pe", "ps", "fcf_yield", "peg_rev_approx", "mktcap_asof", "split_suspect"]:
            f.pop(k, None)
        states.append((d, f))
    S.CUTOFF = pd.Timestamp("2025-12-31")
    return states


def daily_tech(df):
    """与 factor_framework_v3.technicals 同口径的逐日版本。"""
    c, v, o = df["close"], df["volume"], df["open"]
    ma50 = c.rolling(50).mean()
    ma200 = c.rolling(200).mean()
    up_vol = v.where(c >= o, 0.0)
    out = pd.DataFrame(index=df.index)
    out["price"] = c
    out["open"] = o
    out["price_vs_ma200"] = (c / ma200 - 1) * 100
    out["ma50_vs_ma200"] = (ma50 / ma200 - 1) * 100
    out["ma200_slope20"] = (ma200 / ma200.shift(20) - 1) * 100
    out["mom3"] = (c / c.shift(63) - 1) * 100
    out["mom6"] = (c / c.shift(126) - 1) * 100
    out["rsi"] = rsi(c)
    out["dev50"] = (c / ma50 - 1) * 100
    out["vol_ratio"] = v.rolling(20).mean() / v.rolling(60).mean()
    out["up_share"] = up_vol.rolling(60).sum() / v.rolling(60).sum() * 100
    out["vol20"] = c.pct_change().rolling(20).std() * math.sqrt(252) * 100
    out["drawdown252"] = (c / c.rolling(252).max() - 1) * 100
    return out


def compute_books(tech, f_raw, price):
    """与 screen.analyze_asof 同口径：由当日价与财报水平算四本账、红线与过线判定。"""
    f = dict(f_raw)
    if f.get("eps_ttm"):
        e = f["eps_ttm"]
        if e > 0:
            f["pe"] = price / e
    if f.get("shares"):
        f["mktcap_asof"] = f["shares"] * price
    if f.get("mktcap_asof") and f.get("rev_ttm"):
        f["ps"] = f["mktcap_asof"] / f["rev_ttm"]
    if f.get("mktcap_asof") and f.get("fcf_ttm") is not None:
        f["fcf_yield"] = f["fcf_ttm"] / f["mktcap_asof"] * 100
    if f.get("pe") and f.get("rev_growth") and f["rev_growth"] > 0:
        f["peg_rev_approx"] = f["pe"] / f["rev_growth"]
    pe_, ps_, gm_ = f.get("pe"), f.get("ps"), f.get("gross_margin")
    split_suspect = bool((pe_ and pe_ < 5 and ps_ and ps_ < 2) or (ps_ and ps_ < 3 and gm_ and gm_ > 60))

    quality_items = [
        ("毛利率", None, interp(f.get("gross_margin"), [(5, 5), (15, 30), (25, 50), (40, 75), (60, 95)]), 25, ""),
        ("ROE", None, interp(f.get("roe"), [(-10, 0), (0, 25), (5, 45), (15, 75), (25, 95)]), 25, ""),
        ("自由现金流率", None, interp(f.get("fcf_margin"), [(-10, 0), (0, 30), (5, 55), (10, 75), (20, 95)]), 25, ""),
        ("现金流/净利润", None, interp(f.get("fcf_conversion"), [(-0.5, 0), (0, 30), (0.5, 60), (1.0, 85), (1.5, 95)]), 15, ""),
        ("净负债/自由现金流", None, interp(f.get("net_debt_fcf"), [(-1, 100), (0, 95), (1, 85), (3, 60), (5, 30), (8, 5)]), 10, ""),
    ]
    growth_items = [
        ("营收增速TTM", None, interp(f.get("rev_growth"), [(-15, 0), (-5, 15), (0, 35), (5, 50), (10, 65), (20, 82), (35, 95), (60, 100)]), 40, ""),
        ("营收增速加速度", None, interp(f.get("rev_accel_pp"), [(-20, 5), (-10, 25), (-3, 45), (0, 55), (5, 70), (15, 90)]), 25, ""),
        ("EPS增速TTM", None, interp(f.get("eps_growth"), [(-30, 0), (-10, 20), (0, 40), (10, 60), (25, 80), (50, 95)]), 25, ""),
        ("毛利率变化", None, interp(f.get("gross_margin_change_pp"), [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 95)]), 10, ""),
    ]
    valuation_items = [
        ("PE(TTM)", None, interp(f.get("pe"), [(8, 98), (12, 90), (20, 72), (30, 52), (50, 28), (80, 10), (120, 3)]), 35, ""),
        ("PEG近似", None, interp(f.get("peg_rev_approx"), [(0.5, 95), (1.0, 78), (1.5, 58), (2.5, 32), (4.0, 12)]), 25, ""),
        ("市销率PS", None, interp(f.get("ps"), [(0.5, 98), (1, 90), (3, 72), (6, 50), (10, 28), (20, 8)]), 20, ""),
        ("自由现金流收益率", None, interp(f.get("fcf_yield"), [(-2, 0), (0, 12), (1, 32), (3, 58), (5, 78), (8, 95)]), 20, ""),
    ]
    if split_suspect:
        valuation_items = [(n, None, None, w, x) for n, _r, _s, w, x in valuation_items]

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
        ("趋势结构", None, float(trend_score), 35, ""),
        ("中期动量", None, float(momentum_score), 20, ""),
        ("量能确认", None, float(volume_score), 15, ""),
        ("入场位置", None, float(np.mean([location_score, rsi_score])), 20, ""),
        ("波动/回撤风险", None, float(risk_score), 10, ""),
    ]

    quality, _, _ = weighted_book(quality_items)
    growth, _, _ = weighted_book(growth_items)
    valuation, _, _ = weighted_book(valuation_items)
    timing, _, _ = weighted_book(timing_items)

    # 财报过期（按当日判断）：营收期间结束日距今 >180 天则基本面账整体作废
    fund_unreliable = bool(f_raw.get("fund_unreliable"))
    if f_raw.get("rev_period"):
        if (pd.Timestamp(tech["date"]) - pd.Timestamp(f_raw["rev_period"])).days > 180:
            fund_unreliable = True
    if fund_unreliable:
        quality = growth = valuation = None

    redlines, caps = [], []
    if tech["price_vs_ma200"] < 0 and tech["ma50_vs_ma200"] < 0 and tech["mom6"] < -10:
        redlines.append("趋势破位")
        caps.append("排除")
    if f.get("fcf_ttm") is not None and f["fcf_ttm"] < 0 and (f.get("rev_growth") or 0) < 0:
        redlines.append("现金流为负且营收下滑")
        caps.append("排除")
    if (f.get("rev_growth") is not None and f["rev_growth"] < -5) and (f.get("eps_growth") is not None and f["eps_growth"] < -20):
        redlines.append("营收与EPS同时明显下滑")
        caps.append("排除")
    if f.get("current_ratio") is not None and f["current_ratio"] < 0.8:
        redlines.append("流动性偏紧")
        caps.append("只观察")
    if f.get("pe") and f["pe"] > 80 and ((f.get("rev_growth") or 0) < 15 or (f.get("rev_accel_pp") or 0) < -10):
        redlines.append("高估值与增长不匹配")
        caps.append("等价格")
    if tech["rsi"] > 80 and tech["dev50"] > 15:
        redlines.append("短线过热")
        caps.append("不追高")

    books = {"公司质量": quality, "成长与预期": growth, "估值": valuation, "入场时机": timing}
    excluded = any(c == "排除" for c in caps)
    pass_all = (not excluded and all(books[k] is not None and books[k] >= BOOK_MIN[k] for k in BOOK_MIN))
    exit_hit = [k for k in EXIT_MIN if books[k] is not None and books[k] < EXIT_MIN[k]]
    archetype, weights = classify({"sector": S.SECTORS.get("", ""), "industry": ""})
    wmap = {"公司质量": 25, "成长与预期": 32.5, "估值": 25, "入场时机": 17.5}
    rank = None
    if all(v is not None for v in books.values()):
        rank = round(sum(books[k] * wmap[k] for k in books) / sum(wmap.values()), 1)
    return {"books": books, "redlines": redlines, "caps": caps, "excluded": excluded,
            "pass_all": pass_all, "exit_hit": exit_hit, "rank": rank, "overheat": "不追高" in caps,
            "split_suspect": split_suspect, "fund_unreliable": fund_unreliable}


def main():
    print("载入行情与财报状态…", flush=True)
    series = {}   # t -> DataFrame(daily tech)
    opens = {}
    closes = {}
    calendar = None
    for t in TICKERS:
        df = get_history(t)
        tech = daily_tech(df)
        series[t] = tech
        opens[t] = df["open"]
        closes[t] = df["close"]
        calendar = tech.index if calendar is None else calendar.union(tech.index)
    spy = get_history("SPY")
    days = [d for d in calendar if d >= START]
    last_day = days[-1]
    print(f"交易日 {days[0].date()} ~ {last_day.date()}，共 {len(days)} 天", flush=True)

    # 每只股票逐日信号表
    print("逐日重算四本账…", flush=True)
    signals = {}
    first_pass = {}
    for t in TICKERS:
        states = fund_states(t)
        tech = series[t]
        rows = {}
        si = 0
        for d in days:
            while si + 1 < len(states) and states[si + 1][0] <= d:
                si += 1
            row = tech.loc[d]
            if pd.isna(row["price_vs_ma200"]):
                continue
            tech_d = {k: float(row[k]) for k in ["price", "price_vs_ma200", "ma50_vs_ma200", "ma200_slope20",
                                                 "mom3", "mom6", "rsi", "dev50", "vol_ratio", "up_share",
                                                 "vol20", "drawdown252"]}
            tech_d["date"] = str(d.date())
            r = compute_books(tech_d, states[si][1], tech_d["price"])
            r["price"] = tech_d["price"]
            rows[d] = r
            if r["pass_all"] and t not in first_pass:
                first_pass[t] = str(d.date())
        signals[t] = rows
        print(f"  {t} 完成（状态数 {len(states)}）", flush=True)

    # 组合模拟：信号日收盘决策，次日开盘成交
    cash = INIT_CASH
    positions = {}  # t -> {"shares": int, "half_sold": bool, "cost_basis": float}
    trades = []
    equity_curve = []
    pending = []  # (ticker, action, detail) 次日开盘执行

    def equity_at(day, price_src):
        eq = cash
        for t, p in positions.items():
            eq += p["shares"] * float(price_src[t].loc[day])
        return eq

    prev_day = None
    for d in days:
        # 1) 执行昨日信号（今日开盘价）
        if pending:
            eq_open = equity_at(d, opens)
            for (t, action, detail) in pending:
                op = float(opens[t].loc[d])
                if action == "buy":
                    target = TARGET_W * eq_open
                    spend = min(target, cash)
                    sh = int(spend // op)
                    if sh > 0 and spend >= 0.01 * eq_open:
                        cost = sh * op * COST
                        cash -= sh * op + cost
                        if t in positions:
                            positions[t]["shares"] += sh
                            positions[t]["half_sold"] = False
                        else:
                            positions[t] = {"shares": sh, "half_sold": False}
                        trades.append({"日期": str(d.date()), "代码": t, "动作": "买入", "股数": sh,
                                         "价格": round(op, 2), "金额": round(sh * op, 2), "触发": detail})
                elif action == "sell_half" and t in positions:
                    sh = positions[t]["shares"] // 2
                    if sh > 0:
                        proceeds = sh * op
                        cash += proceeds - proceeds * COST
                        positions[t]["shares"] -= sh
                        positions[t]["half_sold"] = True
                        trades.append({"日期": str(d.date()), "代码": t, "动作": "卖出一半", "股数": sh,
                                         "价格": round(op, 2), "金额": round(proceeds, 2), "触发": detail})
                elif action == "sell_all" and t in positions:
                    sh = positions[t]["shares"]
                    proceeds = sh * op
                    cash += proceeds - proceeds * COST
                    trades.append({"日期": str(d.date()), "代码": t, "动作": "全部卖出", "股数": sh,
                                     "价格": round(op, 2), "金额": round(proceeds, 2), "触发": detail})
                    del positions[t]
            pending = []

        # 2) 记录当日权益（收盘）
        equity_curve.append((d, equity_at(d, closes)))

        # 3) 生成今日信号
        sig = {t: signals[t][d] for t in TICKERS if d in signals[t]}
        # 卖出判断
        for t in list(positions.keys()):
            r = sig.get(t)
            if r is None:
                continue
            b = r["books"]
            if r["excluded"]:
                pending.append((t, "sell_all", "红线：" + "、".join(r["redlines"])))
            elif r["exit_hit"]:
                pending.append((t, "sell_all", "跌破退出线：" + "、".join(f"{k}{b[k]:.0f}" for k in r["exit_hit"])))
            elif r["overheat"] and not positions[t]["half_sold"]:
                pending.append((t, "sell_half", "短线过热（RSI>80且距MA50>15%）先减半"))
        # 买入/买回判断
        held = set(positions.keys()) | {p[0] for p in pending if p[1] == "buy"}
        slots = MAX_POS - len([t for t in positions if not any(p[0] == t and p[1] == "sell_all" for p in pending)])
        cands = [(r["rank"] or 0, t) for t, r in sig.items()
                 if r["pass_all"] and t not in positions]
        cands.sort(reverse=True)
        for rank, t in cands:
            if slots <= 0:
                break
            b = sig[t]["books"]
            pending.append((t, "buy", f"四本账过线 质量{b['公司质量']:.0f}/成长{b['成长与预期']:.0f}/估值{b['估值']:.0f}/时机{b['入场时机']:.0f}"))
            slots -= 1
        # 已减半仓位重新过线 → 买回补满
        for t, p in positions.items():
            if p.get("half_sold") and sig.get(t, {}).get("pass_all"):
                pending.append((t, "buy", "过热减半后重新过线，买回补满"))
        prev_day = d

    final_eq = equity_curve[-1][1]
    eq_series = pd.Series(dict(equity_curve))
    max_dd = (eq_series / eq_series.cummax() - 1).min() * 100
    spy_ret = (float(spy["close"].loc[last_day]) / float(spy["close"].loc[pd.Timestamp("2025-12-31")]) - 1) * 100

    # 每只股票盈亏归因
    pnl = {}
    for tr in trades:
        pass
    realized = {}
    buy_cost = {}
    for tr in trades:
        t = tr["代码"]
        amt = tr["股数"] * tr["价格"]
        if tr["动作"] == "买入":
            buy_cost[t] = buy_cost.get(t, 0) + amt
        else:
            realized[t] = realized.get(t, 0) + amt
    open_pos = {}
    for t, p in positions.items():
        mv = p["shares"] * float(closes[t].loc[last_day])
        open_pos[t] = {"股数": p["shares"], "市值": round(mv, 2)}

    summary = {
        "区间": f"{days[0].date()} ~ {last_day.date()}",
        "初始资金": INIT_CASH,
        "最终权益": round(final_eq, 2),
        "累计收益_pct": round((final_eq / INIT_CASH - 1) * 100, 2),
        "最大回撤_pct": round(max_dd, 2),
        "SPY同期_pct": round(spy_ret, 2),
        "交易笔数": len(trades),
        "期末现金": round(cash, 2),
        "期末持仓": open_pos,
        "首次过线日": first_pass,
    }
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "paper_trade_2026_trades.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "trades": trades}, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(base, "paper_trade_2026_equity.csv"), "w", encoding="utf-8") as fh:
        fh.write("date,equity\n")
        for d, e in equity_curve:
            fh.write(f"{d.date()},{e:.2f}\n")
    pd.DataFrame(trades).to_csv(os.path.join(base, "paper_trade_2026_trades.csv"), index=False)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\n交易明细：")
    for tr in trades:
        print(f"{tr['日期']} {tr['代码']:5s} {tr['动作']:5s} {tr['股数']:>7d} 股 @ {tr['价格']:.2f} = {tr['金额']:,.0f}｜{tr['触发']}")


if __name__ == "__main__":
    main()
