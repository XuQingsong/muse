"""
纸面模拟 v8：取消平衡式警戒减仓（全池30只；只在基本面恶化或趋势确认破位时卖出）。

与 v1 的区别只在仓位层，选股信号（v3 四本账+红线、当时可见数据）不变：
- 按标的属性分三层：防守（低波动、现金流稳、质量高）、普通（中波动、高成长、已盈利）、
  进攻（高波动/超高成长/可未盈利）。层预算 防守30%/普通30%/进攻40%（占总权益），
  只是买入水位：层内市值涨过水位不强制减仓，只是不再往该层加新钱（+25%相对缓冲带）；
- 单票目标上限：防守15%/普通12%/进攻5%；买入分三笔（首笔 防守60%/普通40%/进攻25%，
  确认后 +30%，新财报确认后补满）；信号日距MA50>+15% 时当笔减半；
- 减仓分三级：警戒卖1/3、破位（3日确认）再卖剩余一半、基本面逻辑死亡才清仓；
  过热减1/3 且该票 10 个交易日内禁止买回（治 v1 的 UNH 反复抽打）；
- 黑天鹅：SPY 单日<=-4% 或 5 日<=-8% 时，暂停新买入 3 天，进攻仓先降至一半；
- 组合回撤刹车：回撤 -8% 进攻仓砍半，-12% 只允许新买防守仓。
成本 0.1%/笔，信号收盘生成、次日开盘成交。研究练习，不构成投资建议。
"""
import json
import os

import numpy as np
import pandas as pd

import screen_asof_2026_01_01 as S
from factor_scorecard_v2 import get_history
from paper_trade_2026 import TICKERS, daily_tech, fund_states, compute_books
TICKERS = TICKERS + ["MU", "GOOGL"]  # 全池30只：补上2026-01-01已过线的两只

START = pd.Timestamp("2026-01-01")
INIT_CASH = 1_000_000.0
COST = 0.001
BOOK_MIN = {"公司质量": 60, "成长与预期": 55, "估值": 45, "入场时机": 55}
# 分层入场门槛：进攻层允许贵（估值≥30），但成长必须更硬；防守层要求质量与估值纪律
ENTRY_TIER_MIN = {
    "防守": {"公司质量": 70, "成长与预期": 45, "估值": 50, "入场时机": 50},
    "普通": {"公司质量": 60, "成长与预期": 55, "估值": 45, "入场时机": 55},
    "进攻": {"公司质量": 50, "成长与预期": 70, "估值": 30, "入场时机": 50},
}
EXIT_MIN = {"公司质量": 45, "成长与预期": 40, "估值": 35, "入场时机": 40}
TIER_BUDGET = {"防守": 0.30, "普通": 0.30, "进攻": 0.40}
TIER_BAND = 1.25
TIER_CAP = {"防守": 0.12, "普通": 0.12, "进攻": 0.08}
TIER_FIRST = {"防守": 0.50, "普通": 0.40, "进攻": 0.30}
TIER_MAXNAMES = {"防守": 4, "普通": 6, "进攻": 6}
# 注：“流动性偏紧”（流动比率<0.8）不列逻辑死亡：保险、公用事业等行业结构性低于1，
# v2 首跑时 UNH 因此被反复“买入→次日全卖”。它只作警戒级处理。
DEATH_REDLINES = {"现金流为负且营收下滑", "营收与EPS同时明显下滑"}


def assign_tier(vol20, f, books):
    if books["公司质量"] is None or books["成长与预期"] is None:
        return None
    ni, fcf = f.get("ni_ttm"), f.get("fcf_ttm")
    rg = f.get("rev_growth")
    unprofitable = (ni is not None and ni < 0 and fcf is not None and fcf < 0)
    if vol20 > 55 or unprofitable or (rg is not None and rg > 35 and (books["估值"] or 100) < 45):
        return "进攻"
    if (vol20 < 30 and fcf is not None and fcf > 0 and (f.get("fcf_margin") or 0) > 5
            and books["公司质量"] >= 70 and books["成长与预期"] >= 45 and (rg is None or rg >= 0)):
        return "防守"
    return "普通"


def growth_certainty(f, books):
    """成长确定性：不只看增速高，还看增速是否加速、EPS是否跟上。"""
    g = books.get("成长与预期") or 0
    rg, acc, eps = f.get("rev_growth"), f.get("rev_accel_pp"), f.get("eps_growth")
    pts = 0
    pts += 2 if g >= 85 else (1 if g >= 72 else 0)
    pts += 2 if (rg is not None and rg >= 30) else (1 if (rg is not None and rg >= 18) else 0)
    pts += 1 if (acc is not None and acc >= 3) else (-1 if (acc is not None and acc <= -8) else 0)
    pts += 1 if (eps is not None and eps >= 20) else (-1 if (eps is not None and eps < 0) else 0)
    return "高" if pts >= 4 else ("中" if pts >= 2 else "低")


def fund_hold_score(f):
    """基本面'拿得住'资格：0-100。看趋势与现金质量，不看价格。"""
    def pts(v, cuts):
        # cuts: [(门槛, 分数)] 门槛从高到低，最后一项是保底
        if v is None:
            return 8
        for th, sc in cuts:
            if v >= th:
                return sc
        return cuts[-1][1]
    sc = 0
    sc += pts(f.get("rev_q_yoy"), [(20, 15), (10, 12), (0, 8), (-5, 4), (-10**9, 0)])
    acc = f.get("rev_accel_pp")
    sc += 15 if (acc is not None and acc >= 0) else (10 if (acc is not None and acc >= -10) else (5 if (acc is not None and acc >= -20) else 0))
    gm = f.get("gross_margin_change_pp")
    sc += 15 if (gm is not None and gm >= 0) else (10 if (gm is not None and gm >= -3) else (5 if (gm is not None and gm >= -6) else (8 if gm is None else 0)))
    conv = f.get("fcf_conversion")
    sc += 20 if (conv is not None and conv >= 1) else (15 if (conv is not None and conv >= 0.5) else (8 if (conv is not None and conv >= 0) else (8 if conv is None else 0)))
    ndf = f.get("net_debt_fcf")
    sc += 15 if (ndf is not None and ndf <= 0) else (12 if (ndf is not None and ndf <= 3) else (8 if (ndf is not None and ndf <= 5) else (8 if ndf is None else 0)))
    cr = f.get("current_ratio")
    sc += 10 if (cr is not None and cr >= 1.5) else (7 if (cr is not None and cr >= 1) else (4 if (cr is not None and cr >= 0.8) else (5 if cr is None else 0)))
    return sc


def opening_fraction(tier, certainty):
    base = TIER_FIRST[tier]
    if certainty == "高":
        return min(0.75, base + 0.20)
    if certainty == "中":
        return min(0.65, base + 0.08)
    return max(0.20, base - 0.08)


def main():
    print("载入行情与财报状态…", flush=True)
    tech_by, opens, closes = {}, {}, {}
    calendar = None
    for t in TICKERS:
        df = get_history(t)
        tech_by[t] = daily_tech(df)
        opens[t], closes[t] = df["open"], df["close"]
        calendar = tech_by[t].index if calendar is None else calendar.union(tech_by[t].index)
    spy = get_history("SPY")
    spy_close = spy["close"]
    days = [d for d in calendar if d >= START]
    last_day = days[-1]
    day_idx = {d: i for i, d in enumerate(days)}
    print(f"交易日 {days[0].date()} ~ {last_day.date()}，共 {len(days)} 天", flush=True)

    print("逐日重算四本账与分层…", flush=True)
    signals = {}
    for t in TICKERS:
        states = fund_states(t)
        tech = tech_by[t]
        vol60 = closes[t].pct_change().rolling(60).std() * np.sqrt(252) * 100
        rows = {}
        si = 0
        cur_tier = None
        consec_pass = consec_below = consec_tim = consec_tim30 = 0
        for d in days:
            new_state = False
            while si + 1 < len(states) and states[si + 1][0] <= d:
                si += 1
                new_state = True
            row = tech.loc[d]
            if pd.isna(row["price_vs_ma200"]):
                continue
            td = {k: float(row[k]) for k in ["price", "price_vs_ma200", "ma50_vs_ma200", "ma200_slope20",
                                             "mom3", "mom6", "rsi", "dev50", "vol_ratio", "up_share",
                                             "vol20", "drawdown252"]}
            td["date"] = str(d.date())
            r = compute_books(td, states[si][1], td["price"])
            r.update(td)
            # 分层只在新财报状态到达时重估，且用 60 日波动，避免日内波动让层级天天跳
            if new_state or cur_tier is None:
                v60 = vol60.loc[d]
                cur_tier = assign_tier(float(v60) if pd.notna(v60) else td["vol20"], states[si][1], r["books"])
            r["tier"] = cur_tier
            r["certainty"] = growth_certainty(states[si][1], r["books"])
            r["state_idx"] = si
            _fst0 = states[si][1]
            r["rev_growth"] = _fst0.get("rev_growth")
            _fcf0, _cash0 = _fst0.get("fcf_ttm"), _fst0.get("cash") or 0
            r["runway_ok"] = True if (_fcf0 is None or _fcf0 >= 0) else (_cash0 >= 1.0 * abs(_fcf0))
            r["fund_score"] = fund_hold_score(_fst0)
            r["hold_ok"] = r["fund_score"] >= 55
            _qy, _gm, _cv = _fst0.get("rev_q_yoy"), _fst0.get("gross_margin_change_pp"), _fst0.get("fcf_conversion")
            r["fund_bad"] = r["fund_score"] < 40 or ((_qy is not None and _qy < 0) and (_gm is not None and _gm < 0) and (_cv is None or _cv < 0.3))
            em = ENTRY_TIER_MIN.get(cur_tier)
            if em is None:
                r["entry_pass"] = False
            else:
                books_ok = all(r["books"][k] is not None and r["books"][k] >= em[k] for k in em)
                guard = True
                fst = states[si][1]
                if cur_tier == "进攻" and fst.get("fcf_ttm") is not None and fst["fcf_ttm"] < 0:
                    guard = (fst.get("rev_growth") or 0) >= 30 and (fst.get("cash") or 0) >= 1.5 * abs(fst["fcf_ttm"])
                r["entry_pass"] = bool(books_ok and guard and not r.get("excluded", False))
            consec_pass = consec_pass + 1 if r["entry_pass"] else 0
            consec_below = consec_below + 1 if td["price_vs_ma200"] < 0 else 0
            consec_tim = consec_tim + 1 if (r["books"]["入场时机"] is not None and r["books"]["入场时机"] < 35) else 0
            consec_tim30 = consec_tim30 + 1 if (r["books"]["入场时机"] is not None and r["books"]["入场时机"] < 30) else 0
            r["consec_pass"], r["consec_below200"], r["consec_tim35"], r["consec_tim30"] = consec_pass, consec_below, consec_tim, consec_tim30
            rows[d] = r
        signals[t] = rows
        print(f"  {t} 完成", flush=True)

    cash = INIT_CASH
    positions = {}
    trades = []
    equity_curve = []
    pending = []
    last_exit_idx = {}
    last_exit_kind = {}
    peak_eq = INIT_CASH
    swan_until = -1
    last_swan_idx = -100
    dd8_active = False
    dd12_active = False
    invested_sum, invested_days = 0.0, 0

    def equity_at(day, src):
        return cash + sum(p["shares"] * float(src[t].loc[day]) for t, p in positions.items())

    def tier_mv(day):
        mv = {k: 0.0 for k in TIER_BUDGET}
        for t, p in positions.items():
            mv[p["tier_now"]] += p["shares"] * float(closes[t].loc[day])
        return mv

    for d in days:
        i = day_idx[d]
        # ---------- 执行昨日订单（卖先买后） ----------
        if pending:
            eq_open = equity_at(d, opens)
            for od in sorted(pending, key=lambda x: 0 if x["kind"].startswith("sell") else 1):
                t, op = od["t"], float(opens[od["t"]].loc[d])
                if od["kind"] == "sell_all" and t in positions:
                    sh = positions[t]["shares"]
                    cash += sh * op * (1 - COST)
                    trades.append({"日期": str(d.date()), "代码": t, "动作": "全部卖出", "股数": sh, "价格": round(op, 2),
                                   "金额": round(sh * op, 2), "触发": od["reason"], "层": positions[t]["tier_now"]})
                    positions[t]["last_sell_idx"] = i
                    last_exit_idx[t] = i
                    last_exit_kind[t] = od.get("exit_kind", "基本面")
                    del positions[t]
                elif od["kind"] == "sell_frac" and t in positions:
                    sh = int(positions[t]["shares"] * od["frac"])
                    if sh > 0:
                        cash += sh * op * (1 - COST)
                        positions[t]["shares"] -= sh
                        positions[t]["last_sell_idx"] = i
                        trades.append({"日期": str(d.date()), "代码": t, "动作": od["label"], "股数": sh, "价格": round(op, 2),
                                       "金额": round(sh * op, 2), "触发": od["reason"], "层": positions[t]["tier_now"]})
                        if positions[t]["shares"] <= 0:
                            last_exit_idx[t] = i
                            last_exit_kind[t] = "价格"
                            del positions[t]
                elif od["kind"] == "buy_to" and cash > 0:
                    sig = signals[t].get(d)
                    tier_now = (sig or {}).get("tier") or od["tier"]
                    if tier_now is None:
                        continue
                    cap_val = TIER_CAP[tier_now] * eq_open
                    desired = od["cum_frac"] * cap_val
                    cur_val = (positions[t]["shares"] * op) if t in positions else 0.0
                    delta_val = desired - cur_val
                    if od.get("half_size"):
                        delta_val *= 0.5
                    spend = min(delta_val, cash)
                    sh = int(spend // op) if op > 0 else 0
                    if sh > 0 and spend >= 0.005 * eq_open:
                        cash -= sh * op * (1 + COST)
                        if t in positions:
                            _old_sh = positions[t]["shares"]
                            _old_cost = positions[t].get("avg_cost") or op
                            positions[t]["avg_cost"] = (_old_sh * _old_cost + sh * op) / (_old_sh + sh)
                            positions[t]["shares"] += sh
                            positions[t]["filled_frac"] = max(positions[t]["filled_frac"], od["cum_frac"] if not od.get("half_size") else od["cum_frac"] * 0.5)
                        else:
                            positions[t] = {"shares": sh, "tier_now": tier_now, "tier_entry": tier_now,
                                            "filled_frac": od["cum_frac"] if not od.get("half_size") else od["cum_frac"] * 0.5,
                                            "first_frac": od["cum_frac"], "certainty": od.get("certainty", "中"),
                                            "stage": 0, "entry_idx": i, "entry_state": (sig or {}).get("state_idx", 0), "avg_cost": op,
                                            "last_sell_idx": -100, "peak_price": op}
                        trades.append({"日期": str(d.date()), "代码": t, "动作": od["label"], "股数": sh, "价格": round(op, 2),
                                       "金额": round(sh * op, 2), "触发": od["reason"], "层": tier_now})
            pending = []

        # ---------- 当日权益与风控状态 ----------
        eq_close = equity_at(d, closes)
        equity_curve.append((d, eq_close))
        peak_eq = max(peak_eq, eq_close)
        dd = (eq_close / peak_eq - 1) * 100
        invested = eq_close - cash
        invested_sum += invested / eq_close if eq_close else 0
        invested_days += 1
        spy_today = spy_close.loc[d] if d in spy_close.index else None
        spy_ret1 = spy_ret5 = None
        if spy_today is not None:
            hist = spy_close.loc[:d]
            if len(hist) >= 2:
                spy_ret1 = (hist.iloc[-1] / hist.iloc[-2] - 1) * 100
            if len(hist) >= 6:
                spy_ret5 = (hist.iloc[-1] / hist.iloc[-6] - 1) * 100
        swan_trigger = (spy_ret1 is not None and spy_ret1 <= -4) or (spy_ret5 is not None and spy_ret5 <= -8)
        if dd <= -8 and not dd8_active:
            dd8_active = True
            for t, p in list(positions.items()):
                if p["tier_now"] == "进攻":
                    pending.append({"t": t, "kind": "sell_frac", "frac": 1 / 3, "label": "回撤刹车减仓",
                                    "reason": f"组合回撤 {dd:.1f}% 触发 -8% 刹车，进攻仓减1/3（按进攻层定位只降速不砍半）"})
        if dd > -4:
            dd8_active = False
        dd12_active = dd <= -12 or (dd12_active and dd < -6)
        if swan_trigger and i - last_swan_idx > 10:
            last_swan_idx = i
            swan_until = i + 3
            for t, p in list(positions.items()):
                if p["tier_now"] == "进攻":
                    pending.append({"t": t, "kind": "sell_frac", "frac": 0.5, "label": "黑天鹅减仓",
                                    "reason": "大盘急跌触发黑天鹅条款，进攻仓先降至一半"})

        def buy_limit(tier, mv_, eq_):
            lim = TIER_BUDGET[tier] * TIER_BAND * eq_
            inv_ratio = (eq_ - cash) / eq_ if eq_ else 0
            if tier != "进攻" and inv_ratio < 0.85:
                unused_attack = max(0.0, TIER_BUDGET["进攻"] * TIER_BAND * eq_ - mv_["进攻"])
                lim += 0.75 * unused_attack
            return lim

        # ---------- 生成今日信号 ----------
        sig = {t: signals[t][d] for t in TICKERS if d in signals[t]}
        buys_blocked = i <= swan_until
        # 持仓分级卖出（参数按层分别设定；层预算只限制买入，涨超水位绝不被动减仓）
        sold_today = set()
        for t in list(positions.keys()):
            r = sig.get(t)
            if r is None:
                continue
            p = positions[t]
            p["tier_now"] = r["tier"] or p["tier_now"]
            tier_p = p["tier_now"]
            b = r["books"]
            p["peak_price"] = max(p.get("peak_price") or 0, r["price"])
            q, g = b["公司质量"], b["成长与预期"]
            death_q, death_g = (50, 35) if tier_p == "防守" else (45, 40)
            death = any(x in DEATH_REDLINES for x in r["redlines"]) or \
                (q is not None and q < death_q) or (g is not None and g < death_g)
            if tier_p == "进攻" and not death:
                if (r.get("rev_growth") is not None and r["rev_growth"] < 0) or (not r.get("runway_ok", True)):
                    death = True
            if r.get("fund_bad"):
                death = True
            if death:
                why = "、".join(r["redlines"]) or "基本面恶化（营收转负增长/现金跑道不足/账本跌破退出线）"
                pending.append({"t": t, "kind": "sell_all", "exit_kind": "基本面", "reason": f"逻辑死亡（{tier_p}层标准）：{why}"})
                sold_today.add(t)
                continue
            if tier_p == "进攻" and p["peak_price"] > 0 and r["price"] <= 0.60 * p["peak_price"] and r["price_vs_ma200"] < 0:
                pending.append({"t": t, "kind": "sell_all", "exit_kind": "价格", "reason": "进攻仓灾难止损：自持仓高点回撤≥40%且跌破200日线"})
                sold_today.add(t)
                continue
            if (tier_p == "进攻" and p.get("avg_cost") and r["price"] >= 2.0 * p["avg_cost"]
                    and (r["dev50"] < 3 or (b["入场时机"] is not None and b["入场时机"] < 55))
                    and i - p.get("last_sell_idx", -100) >= 20 and p["stage"] == 0):
                pending.append({"t": t, "kind": "sell_frac", "frac": 0.25, "label": "分批止盈",
                                "reason": f"进攻仓较成本翻倍且动能降温，先锁25%（成本约{p['avg_cost']:.0f}美元，现价{r['price']:.0f}美元）"})
                sold_today.add(t)
                continue
            # 警戒只观察、不卖出：分数回落或仓位失衡都不是卖出理由。
            # 只有基本面死亡，或“新财报确认走弱 + 趋势跌破”才动手，避免平衡式减仓。
            if tier_p == "防守":
                alert = False
                broke = r["consec_below200"] >= 10
                alert_frac, broke_frac, need_clear = 0.0, 1 / 3, 5
            elif tier_p == "进攻":
                alert = False
                broke = r["consec_below200"] >= 7 or r.get("consec_tim30", 0) >= 5
                alert_frac, broke_frac, need_clear = 0.0, 1 / 3, 5
            else:
                alert = (r["state_idx"] > p["entry_state"] and ((q is not None and q < 55) or (g is not None and g < 50)) and r["dev50"] < 0)
                broke = ("趋势破位" in r["redlines"]) or r["consec_below200"] >= 3 or r["consec_tim35"] >= 3
                alert_frac, broke_frac, need_clear = 0.25, 0.5, 3
            if broke and p["stage"] < 2:
                p["stage"] = 2
                pending.append({"t": t, "kind": "sell_frac", "frac": broke_frac, "label": "破位减仓",
                                "reason": f"{tier_p}层破位确认（按该层宽容度），减{round(broke_frac * 100)}%"})
                sold_today.add(t)
                continue
            if alert and p["stage"] == 0:
                p["stage"] = 1
                p["clear_streak"] = 0
                pending.append({"t": t, "kind": "sell_frac", "frac": alert_frac, "label": "警戒减仓",
                                "reason": f"{tier_p}层确认性减仓（非仓位平衡）：新财报后基本面走弱且跌破50日线，先减{round(alert_frac * 100)}%"})
                sold_today.add(t)
                continue
            if p["stage"] > 0:
                if (not alert) and r["price_vs_ma200"] > 0 and r["entry_pass"]:
                    p["clear_streak"] = p.get("clear_streak", 0) + 1
                    if p["clear_streak"] >= need_clear:
                        p["stage"] = 0
                        p["clear_streak"] = 0
                else:
                    p["clear_streak"] = 0

        if not buys_blocked:
            mv = tier_mv(d)
            # 已有持仓的分级补仓（优先于新开仓）
            for t, p in list(positions.items()):
                r = sig.get(t)
                if r is None or not r["entry_pass"] or r["tier"] is None:
                    continue
                if t in sold_today or r["overheat"] or r.get("fund_bad"):
                    continue
                if i - p.get("last_sell_idx", -100) < 10:
                    continue  # 卖出冷却期内不买回
                if dd12_active and r["tier"] != "防守":
                    continue
                head = buy_limit(r["tier"], mv, eq_close) - mv[r["tier"]]
                if head <= 0:
                    continue
                nxt = None
                first_frac = p.get("first_frac", TIER_FIRST[r["tier"]])
                held_days = i - p["entry_idx"]
                inv_ratio = (eq_close - cash) / eq_close if eq_close else 0
                need_days = 3 if (p.get("certainty") == "高" or inv_ratio < 0.60) else 5
                if p["filled_frac"] < first_frac + 0.30 - 1e-9:
                    pullback = abs(r["dev50"]) <= 3 and r["price_vs_ma200"] > 0
                    if (held_days >= need_days and r["consec_pass"] >= need_days) or pullback:
                        nxt = (min(0.85, first_frac + 0.30), "确认加仓")
                elif p["filled_frac"] < 1.0 - 1e-9:
                    if r["state_idx"] > p["entry_state"] or (held_days >= 15 and r["consec_pass"] >= 10):
                        nxt = (1.0, "确认补满")
                if nxt:
                    rb = r["books"]
                    pending.append({"t": t, "kind": "buy_to", "cum_frac": nxt[0], "tier": r["tier"],
                                      "label": nxt[1], "certainty": p.get("certainty", "中"), "half_size": r["dev50"] > 15,
                                      "reason": f"{r['tier']}层分级加仓至{nxt[0]*100:.0f}%（质量{rb['公司质量']:.0f}/成长{rb['成长与预期']:.0f}/估值{rb['估值']:.0f}/时机{rb['入场时机']:.0f}）"})
            # 新开仓
            counts = {k: 0 for k in TIER_BUDGET}
            for p in positions.values():
                counts[p["tier_now"]] += 1
            pending_new = {od["t"] for od in pending if od["kind"] == "buy_to"}
            cands = sorted([(r["rank"] or 0, t, False) for t, r in sig.items()
                            if r["entry_pass"] and not r["overheat"] and r.get("fund_score", 0) >= 45 and r["tier"] is not None and t not in positions and t not in pending_new
                            and i - last_exit_idx.get(t, -100) >= 10], reverse=True)
            _have = {t for _, t, _ in cands}
            cands += sorted([((r["rank"] or 0) - 3, t, True) for t, r in sig.items()
                             if t not in _have and t not in positions and t not in pending_new
                             and i - last_exit_idx.get(t, -100) >= 10 and last_exit_kind.get(t) == "价格"
                             and r.get("hold_ok") and r["tier"] in ("普通", "进攻") and r["dev50"] > 0
                             and (r["books"]["入场时机"] or 0) >= 50 and (r["books"]["估值"] or 0) >= ENTRY_TIER_MIN[r["tier"]]["估值"] - 10], reverse=True)
            planned_mv = dict(mv)
            inv_ratio = (eq_close - cash) / eq_close if eq_close else 0
            ordered = [x for x in cands if sig[x[1]]["tier"] == "进攻"] + [x for x in cands if sig[x[1]]["tier"] != "进攻"]
            for rank, t, is_re in ordered:
                r = sig[t]
                tier = r["tier"]
                if counts[tier] >= TIER_MAXNAMES[tier] or len(positions) + len([o for o in pending if o["kind"] == "buy_to"]) >= 12:
                    continue
                if tier == "防守" and not is_re and r.get("fund_score", 0) < 55:
                    continue
                if dd12_active and tier != "防守":
                    continue
                first_frac = opening_fraction(tier, r.get("certainty", "中"))
                if is_re:
                    first_frac = round(first_frac * 0.8, 3)
                if inv_ratio < 0.55 and r.get("certainty") != "低":
                    first_frac = min(0.80, first_frac + 0.10)
                first_val = first_frac * TIER_CAP[tier] * eq_close
                if planned_mv[tier] + first_val > buy_limit(tier, planned_mv, eq_close):
                    continue
                pending.append({"t": t, "kind": "buy_to", "cum_frac": first_frac, "tier": tier, "certainty": r.get("certainty", "中"),
                                  "label": "回补买入" if is_re else "试仓买入", "half_size": r["dev50"] > 15,
                                  "reason": (f"{tier}层回补（此前价格型退出，基本面持有资格{r.get('fund_score',0):.0f}分仍过关，股价站回50日线），首笔{first_frac*100:.0f}%"
                                             if is_re else
                                             f"{tier}层试仓，成长确定性{r.get('certainty','中')}，首笔{first_frac*100:.0f}%（质量{r['books']['公司质量']:.0f}/成长{r['books']['成长与预期']:.0f}/估值{r['books']['估值']:.0f}/时机{r['books']['入场时机']:.0f}）")})
                counts[tier] += 1
                planned_mv[tier] += first_val

    final_eq = equity_curve[-1][1]
    eq_series = pd.Series(dict(equity_curve))
    max_dd = (eq_series / eq_series.cummax() - 1).min() * 100
    spy_ret = (float(spy_close.loc[last_day]) / float(spy_close.loc[pd.Timestamp("2025-12-31")]) - 1) * 100
    open_pos = {t: {"股数": p["shares"], "市值": round(p["shares"] * float(closes[t].loc[last_day]), 2), "层": p["tier_now"]}
                for t, p in positions.items()}
    summary = {
        "区间": f"{days[0].date()} ~ {last_day.date()}", "初始资金": INIT_CASH,
        "最终权益": round(final_eq, 2), "累计收益_pct": round((final_eq / INIT_CASH - 1) * 100, 2),
        "最大回撤_pct": round(max_dd, 2), "SPY同期_pct": round(spy_ret, 2),
        "交易笔数": len(trades), "平均仓位_pct": round(invested_sum / max(invested_days, 1) * 100, 1),
        "期末现金": round(cash, 2), "期末持仓": open_pos,
    }
    base = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(base, "paper_trade_v8full_trades.json"), "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "trades": trades}, fh, ensure_ascii=False, indent=2)
    pd.DataFrame(trades).to_csv(os.path.join(base, "paper_trade_v8full_trades.csv"), index=False)
    with open(os.path.join(base, "paper_trade_v8full_equity.csv"), "w", encoding="utf-8") as fh:
        fh.write("date,equity\n")
        for d, e in equity_curve:
            fh.write(f"{d.date()},{e:.2f}\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    for tr in trades:
        print(f"{tr['日期']} {tr['代码']:5s} {tr['动作']:5s} {tr['股数']:>7d} 股 @ {tr['价格']:.2f} = {tr['金额']:,.0f}｜[{tr['层']}] {tr['触发']}")


if __name__ == "__main__":
    main()
