"""
站在最新收盘（2026-10-07 美股收盘，本地时间 10-08 看）用 v18 同一套规则
给 30 只标的做"今天从零开始会怎么买"的纸面部署快照。
口径与 paper_trade_v18_full30.py 完全一致：四本账、ENTRY_TIER_MIN 分层门槛、
assign_tier/growth_certainty/fund_hold_score/opening_fraction/insider_boost 原样复用，
组合层按 TIER_BUDGET×TIER_BAND 买入上限、TIER_CAP 单票上限、TIER_MAXNAMES 名额、
"距 MA50>+15% 首笔减半"执行。研究练习，不构成投资建议。
"""
import json
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from factor_scorecard_v2 import get_history
from paper_trade_2026 import TICKERS, daily_tech, fund_states, compute_books
import paper_trade_v18_full30 as V

TICKERS30 = TICKERS + ["MU", "GOOGL"]
EQ = 1_000_000.0

def latest_signal(t):
    df = get_history(t)
    tech = daily_tech(df)
    closes = df["close"]
    d = tech.index[-1]
    states = fund_states(t)
    si = 0
    for i, (sd, _) in enumerate(states):
        if sd <= d:
            si = i
    f = states[si][1]
    row = tech.loc[d]
    td = {k: float(row[k]) for k in ["price", "price_vs_ma200", "ma50_vs_ma200", "ma200_slope20",
                                     "mom3", "mom6", "rsi", "dev50", "vol_ratio", "up_share",
                                     "vol20", "drawdown252"]}
    td["date"] = str(d.date())
    r = compute_books(td, f, td["price"])
    r.update(td)
    v60 = closes.pct_change().rolling(60).std().iloc[-1] * np.sqrt(252) * 100
    tier = V.assign_tier(float(v60) if pd.notna(v60) else td["vol20"], f, r["books"])
    r["tier"] = tier
    r["certainty"] = V.growth_certainty(f, r["books"])
    r["fund_score"] = V.fund_hold_score(f)
    r["rev_growth"] = f.get("rev_growth")
    r["rev_accel"] = f.get("rev_accel_pp")
    r["state_date"] = str(states[si][0].date())
    em = V.ENTRY_TIER_MIN.get(tier)
    if em is None:
        r["entry_pass"] = False
    else:
        _val = r["books"]["估值"]
        if tier == "进攻" and (r["books"]["成长与预期"] or 0) >= 70 and _val is not None:
            _rg = f.get("rev_growth")
            if _rg is not None:
                _val = min(65.0, _val + max(0.0, min(20.0, (_rg - 15) / 3.0)))
        r["val_adj"] = _val
        books_ok = all(((_val if k == "估值" else r["books"][k]) is not None
                        and (_val if k == "估值" else r["books"][k]) >= em[k]) for k in em)
        guard = True
        if tier == "进攻" and f.get("fcf_ttm") is not None and f["fcf_ttm"] < 0:
            guard = (f.get("rev_growth") or 0) >= 30 and (f.get("cash") or 0) >= 1.5 * abs(f["fcf_ttm"])
        r["entry_pass"] = bool(books_ok and guard and not r.get("excluded", False))
    r["insider_boost"] = V.insider_boost(t, str(d.date()))
    return t, r

def buy_limit(tier, mv, eq_, cash):
    lim = V.TIER_BUDGET[tier] * V.TIER_BAND * eq_
    inv_ratio = (eq_ - cash) / eq_ if eq_ else 0
    if tier != "进攻" and inv_ratio < 0.85:
        unused_attack = max(0.0, V.TIER_BUDGET["进攻"] * V.TIER_BAND * eq_ - mv["进攻"])
        lim += 0.75 * unused_attack
    return lim

def main():
    sigs = {}
    for t in TICKERS30:
        try:
            _, r = latest_signal(t)
            sigs[t] = r
            print(f"OK {t}", flush=True)
        except Exception as e:
            print(f"FAIL {t}: {type(e).__name__} {str(e)[:100]}", flush=True)

    # 大盘状态：SPY 趋势 + 近5日涨跌（黑天鹅暂停买入检查）
    spy = get_history("SPY")
    sc = spy["close"]
    spy_row = {
        "date": str(spy.index[-1].date()), "close": float(sc.iloc[-1]),
        "vs_ma200_pct": float((sc.iloc[-1] / sc.rolling(200).mean().iloc[-1] - 1) * 100),
        "ret1d_pct": float((sc.iloc[-1] / sc.iloc[-2] - 1) * 100),
        "ret5d_pct": float((sc.iloc[-1] / sc.iloc[-6] - 1) * 100),
    }

    # 候选：entry_pass、无过热、fund_score>=45；防守层新开还须 fund_score>=55
    cands = [(r.get("rank") or 0, t) for t, r in sigs.items()
             if r.get("entry_pass") and not r.get("overheat") and r.get("fund_score", 0) >= 45 and r.get("tier")]
    cands.sort(reverse=True)
    ordered = [x for x in cands if sigs[x[1]]["tier"] == "进攻"] + [x for x in cands if sigs[x[1]]["tier"] != "进攻"]

    cash = EQ
    mv = {"防守": 0.0, "普通": 0.0, "进攻": 0.0}
    counts = {"防守": 0, "普通": 0, "进攻": 0}
    plan, skipped = [], []
    for rank, t in ordered:
        r = sigs[t]
        tier = r["tier"]
        if counts[tier] >= V.TIER_MAXNAMES[tier] or sum(counts.values()) >= 12:
            skipped.append((t, "名额已满")); continue
        if tier == "防守" and r.get("fund_score", 0) < 55:
            skipped.append((t, "防守层要求基本面持有分≥55")); continue
        frac = V.opening_fraction(tier, r.get("certainty", "中"))
        if r.get("insider_boost", 1.0) > 1.0:
            frac = min(0.80, frac * r["insider_boost"])
        val = frac * V.TIER_CAP[tier] * EQ
        half = r["dev50"] > 15
        if half:
            val *= 0.5
        if mv[tier] + val > buy_limit(tier, mv, EQ, cash):
            skipped.append((t, f"{tier}层预算上限已满")); continue
        if val > cash:
            skipped.append((t, "现金不足")); continue
        cash -= val
        mv[tier] += val
        counts[tier] += 1
        plan.append({"ticker": t, "tier": tier, "rank": r.get("rank"),
                     "books": {k: (None if v is None else round(v, 1)) for k, v in r["books"].items()},
                     "certainty": r["certainty"], "fund_score": round(r["fund_score"], 1),
                     "price": round(r["price"], 2), "dev50_pct": round(r["dev50"], 1),
                     "rev_growth": r["rev_growth"], "rev_accel_pp": r["rev_accel"],
                     "first_frac_of_cap": round(frac, 3), "half_due_to_dev50": half,
                     "insider_boost": r.get("insider_boost", 1.0),
                     "buy_usd": round(val), "target_full_pos_usd": round(V.TIER_CAP[tier] * EQ),
                     "state_date": r["state_date"], "signal_date": r["date"]})

    out = {"signal_asof": spy_row["date"], "spy": spy_row, "plan": plan, "skipped": skipped,
           "all": {t: {"tier": r.get("tier"), "entry_pass": r.get("entry_pass"),
                       "overheat": r.get("overheat"), "rank": r.get("rank"),
                       "books": {k: (None if v is None else round(v, 1)) for k, v in r["books"].items()},
                       "certainty": r.get("certainty"), "fund_score": round(r.get("fund_score", 0), 1),
                       "price": round(r["price"], 2), "dev50_pct": round(r["dev50"], 1),
                       "state_date": r.get("state_date")}
                   for t, r in sigs.items()}}
    json.dump(out, open("today_plan_2026-10-08.json", "w"), ensure_ascii=False, indent=1, default=str)
    print("\n=== SPY ===", spy_row)
    print(f"\n=== 今日纸面部署（权益 {EQ:,.0f} 美元，信号日 {spy_row['date']} 收盘）===")
    for p in plan:
        print(f"{p['ticker']:>5} [{p['tier']}] 首笔 {p['buy_usd']:>9,} 美元（满仓目标 {p['target_full_pos_usd']:,}，"
              f"首笔占上限 {p['first_frac_of_cap']*100:.0f}%{'，距50日线过远减半' if p['half_due_to_dev50'] else ''}）"
              f" 四本账 {p['books']} 确定性{p['certainty']} 持有分{p['fund_score']}")
    print(f"\n合计建仓 {EQ-cash:,.0f} 美元（{counts}），留现金 {cash:,.0f} 美元")
    print("跳过:", skipped)

if __name__ == "__main__":
    main()
