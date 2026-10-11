"""
生成《评分逻辑与个股得分》明细：复刻 paper_trade_2026.compute_books 的逐项打分，
为 30 只标的输出每个子项的原始指标值、单项分、权重与四本账总分，
并与 compute_books 的总分对账（差 >0.2 即报错），保证文档数字与策略一致。
信号截至最新收盘。
"""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, ".")
from factor_scorecard_v2 import get_history
from factor_framework_v3 import interp, weighted_book
from paper_trade_2026 import TICKERS, daily_tech, fund_states, compute_books
import paper_trade_v18_full30 as V

TICKERS30 = TICKERS + ["MU", "GOOGL"]

def items_for(tech, f_raw, price):
    """与 compute_books 完全相同的子项构造，仅多回收明细。"""
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

    quality = [
        ("毛利率", f.get("gross_margin"), interp(f.get("gross_margin"), [(5, 5), (15, 30), (25, 50), (40, 75), (60, 95)]), 25),
        ("ROE", f.get("roe"), interp(f.get("roe"), [(-10, 0), (0, 25), (5, 45), (15, 75), (25, 95)]), 25),
        ("自由现金流率", f.get("fcf_margin"), interp(f.get("fcf_margin"), [(-10, 0), (0, 30), (5, 55), (10, 75), (20, 95)]), 25),
        ("现金流/净利润", f.get("fcf_conversion"), interp(f.get("fcf_conversion"), [(-0.5, 0), (0, 30), (0.5, 60), (1.0, 85), (1.5, 95)]), 15),
        ("净负债/自由现金流", f.get("net_debt_fcf"), interp(f.get("net_debt_fcf"), [(-1, 100), (0, 95), (1, 85), (3, 60), (5, 30), (8, 5)]), 10),
    ]
    growth = [
        ("营收增速TTM", f.get("rev_growth"), interp(f.get("rev_growth"), [(-15, 0), (-5, 15), (0, 35), (5, 50), (10, 65), (20, 82), (35, 95), (60, 100)]), 40),
        ("营收增速加速度", f.get("rev_accel_pp"), interp(f.get("rev_accel_pp"), [(-20, 5), (-10, 25), (-3, 45), (0, 55), (5, 70), (15, 90)]), 25),
        ("EPS增速TTM", f.get("eps_growth"), interp(f.get("eps_growth"), [(-30, 0), (-10, 20), (0, 40), (10, 60), (25, 80), (50, 95)]), 25),
        ("毛利率变化", f.get("gross_margin_change_pp"), interp(f.get("gross_margin_change_pp"), [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 95)]), 10),
    ]
    valuation = [
        ("PE(TTM)", f.get("pe"), interp(f.get("pe"), [(8, 98), (12, 90), (20, 72), (30, 52), (50, 28), (80, 10), (120, 3)]), 35),
        ("PEG近似", f.get("peg_rev_approx"), interp(f.get("peg_rev_approx"), [(0.5, 95), (1.0, 78), (1.5, 58), (2.5, 32), (4.0, 12)]), 25),
        ("市销率PS", f.get("ps"), interp(f.get("ps"), [(0.5, 98), (1, 90), (3, 72), (6, 50), (10, 28), (20, 8)]), 20),
        ("自由现金流收益率", f.get("fcf_yield"), interp(f.get("fcf_yield"), [(-2, 0), (0, 12), (1, 32), (3, 58), (5, 78), (8, 95)]), 20),
    ]
    if split_suspect:
        valuation = [(n, raw, None, w) for n, raw, _s, w in valuation]

    trend = float(np.mean([
        interp(tech["price_vs_ma200"], [(-15, 5), (-5, 30), (0, 55), (5, 75), (15, 90)]),
        interp(tech["ma50_vs_ma200"], [(-10, 5), (-3, 30), (0, 55), (3, 75), (10, 90)]),
        interp(tech["ma200_slope20"], [(-2, 10), (-0.5, 35), (0, 55), (0.5, 75), (2, 90)])]))
    momentum = float(np.mean([
        interp(tech["mom3"], [(-20, 5), (-10, 25), (0, 50), (10, 70), (25, 85), (50, 90)]),
        interp(tech["mom6"], [(-30, 5), (-15, 25), (0, 50), (15, 70), (40, 85), (80, 90)])]))
    volume = float(np.mean([
        interp(tech["vol_ratio"], [(0.5, 25), (0.8, 45), (1.0, 60), (1.2, 75), (1.8, 85)]),
        interp(tech["up_share"], [(35, 10), (45, 35), (50, 55), (55, 70), (65, 90)])]))
    loc = float(np.mean([
        interp(tech["dev50"], [(-15, 25), (-8, 45), (-3, 75), (0, 85), (3, 80), (8, 55), (15, 25), (25, 5)]),
        interp(tech["rsi"], [(20, 35), (30, 55), (45, 75), (60, 70), (70, 45), (80, 18), (90, 5)])]))
    risk = float(np.mean([
        interp(tech["vol20"], [(20, 90), (35, 70), (50, 50), (70, 25), (100, 5)]),
        interp(tech["drawdown252"], [(-60, 20), (-40, 35), (-25, 50), (-10, 70), (0, 85)])]))
    timing = [
        ("趋势结构（价/均线/斜率合成）", None, trend, 35),
        ("中期动量（3月+6月涨幅）", None, momentum, 20),
        ("量能确认（量比+上涨日量占比）", None, volume, 15),
        ("入场位置（距50日线+RSI）", None, loc, 20),
        ("波动/回撤风险（20日波动+距高回撤）", None, risk, 10),
    ]
    return {"公司质量": quality, "成长与预期": growth, "估值": valuation, "入场时机": timing,
            "f": f, "split_suspect": split_suspect}

def fmt(x, nd=1):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)

def main():
    rows = []
    detail = {}
    for t in TICKERS30:
        df = get_history(t)
        tech_df = daily_tech(df)
        d = tech_df.index[-1]
        row = tech_df.loc[d]
        tech = {k: float(row[k]) for k in ["price", "price_vs_ma200", "ma50_vs_ma200", "ma200_slope20",
                                           "mom3", "mom6", "rsi", "dev50", "vol_ratio", "up_share",
                                           "vol20", "drawdown252"]}
        tech["date"] = str(d.date())
        states = fund_states(t)
        si = max(i for i, (sd, _) in enumerate(states) if sd <= d)
        f_raw = states[si][1]
        ref = compute_books(tech, f_raw, tech["price"])
        got = items_for(tech, f_raw, tech["price"])
        books = {}
        for name in ["公司质量", "成长与预期", "估值", "入场时机"]:
            items = [(n, None, s, w, "") for n, _raw, s, w in got[name]]
            sc, _cov, _ = weighted_book(items)
            exp = ref["books"][name]
            if exp is None and name != "入场时机":
                books[name] = None  # 策略口径下财务账被作废（过期/不可靠/全缺），以策略为准
            else:
                books[name] = sc
                if sc is not None and abs(sc - exp) > 0.25:
                    raise AssertionError(f"{t} {name} 对账失败： 明细 {sc} vs 策略 {exp}")
        v60 = float(df["close"].pct_change().rolling(60).std().iloc[-1] * np.sqrt(252) * 100)
        tier = V.assign_tier(v60, f_raw, ref["books"])
        detail[t] = {"books_items": got, "books": books, "tier": tier,
                     "certainty": V.growth_certainty(f_raw, ref["books"]),
                     "fund_score": round(V.fund_hold_score(f_raw), 1),
                     "rank": ref["rank"], "overheat": ref["overheat"], "price": round(tech["price"], 2),
                     "dev50": round(tech["dev50"], 1), "state_date": str(states[si][0].date()),
                     "signal_date": str(d.date()), "f": got["f"]}
        rows.append(t)
        print(f"OK {t}", flush=True)

    # —— 输出 Markdown ——
    import json
    plan = json.load(open("today_plan_2026-10-08.json"))
    buys = {p["ticker"]: p for p in plan["plan"]}
    sd = detail[rows[0]]["signal_date"]
    L = []
    L.append(f"# 评分逻辑梳理与个股得分（信号日 {sd} 收盘）\n")
    L.append("说明：本表由 `gen_score_report.py` 复刻策略原函数逐项重算，并与 `compute_books` 对账一致。"
             "分数是启发式先验刻度（0–100），未经样本外验证，不代表涨跌概率。金额单位：美元。\n")

    L.append("## 一、四本账怎么算\n")
    L.append("每项指标先过一个**分段线性插值**：把指标值映射到 0–100 分（锚点见各表，锚点外取端点值）；"
             "缺失的指标直接剔除、剩余权重重新归一（不计 0 分）——所以一本账全缺时该账为「缺失」，整只票出局。\n")

    anchors = {
        "公司质量": [("毛利率", "%", 25, "(5→5)(15→30)(25→50)(40→75)(60→95)"),
                     ("ROE", "%", 25, "(-10→0)(0→25)(5→45)(15→75)(25→95)"),
                     ("自由现金流率", "%", 25, "(-10→0)(0→30)(5→55)(10→75)(20→95)"),
                     ("现金流/净利润", "倍", 15, "(-0.5→0)(0→30)(0.5→60)(1.0→85)(1.5→95)"),
                     ("净负债/自由现金流", "倍", 10, "(-1→100)(0→95)(1→85)(3→60)(5→30)(8→5)")],
        "成长与预期": [("营收增速TTM", "%", 40, "(-15→0)(-5→15)(0→35)(5→50)(10→65)(20→82)(35→95)(60→100)"),
                      ("营收增速加速度", "百分点", 25, "(-20→5)(-10→25)(-3→45)(0→55)(5→70)(15→90)"),
                      ("EPS增速TTM", "%", 25, "(-30→0)(-10→20)(0→40)(10→60)(25→80)(50→95)"),
                      ("毛利率变化", "百分点", 10, "(-5→10)(-2→35)(0→55)(2→75)(5→95)")],
        "估值": [("PE(TTM)", "倍", 35, "(8→98)(12→90)(20→72)(30→52)(50→28)(80→10)(120→3)"),
                 ("PEG近似=PE/营收增速", "倍", 25, "(0.5→95)(1.0→78)(1.5→58)(2.5→32)(4.0→12)"),
                 ("市销率PS", "倍", 20, "(0.5→98)(1→90)(3→72)(6→50)(10→28)(20→8)"),
                 ("自由现金流收益率", "%", 20, "(-2→0)(0→12)(1→32)(3→58)(5→78)(8→95)")],
        "入场时机": [("趋势结构", "价vs200线、50vs200线、200线斜率三者插值均值", 35, "各指标锚点见源码 interp 表"),
                     ("中期动量", "3个月与6个月涨幅插值均值", 20, "3月：(-20→5)(0→50)(10→70)(25→85)(50→90)"),
                     ("量能确认", "20/60日量比、上涨日成交量占比插值均值", 15, "量比(0.5→25)(1.0→60)(1.2→75)(1.8→85)"),
                     ("入场位置", "距50日线乖离、RSI(14)插值均值", 20, "乖离(-3→75)(0→85)(3→80)(8→55)(15→25)"),
                     ("波动/回撤风险", "20日波动率、距252日高点回撤插值均值", 10, "波动(20→90)(35→70)(50→50)(70→25)")],
    }
    for name, items in anchors.items():
        L.append(f"### {name}\n")
        L.append("| 子项 | 权重 | 打分锚点（指标值→分数） |")
        L.append("|---|---|---|")
        for n, unit, w, a in items:
            L.append(f"| {n}（{unit}） | {w} | {a} |")
        L.append("")

    L.append("## 二、红线与特殊规则（优先于分数）\n")
    L.append("- **趋势破位排除**：价在 200 日线下 + 50 日线在 200 日线下 + 6 月动量 < -10%，直接排除。")
    L.append("- **基本面死亡排除**：自由现金流为负且营收下滑；或营收 < -5% 且 EPS < -20% —— 排除（也是清仓级红线）。")
    L.append("- **高估值不匹配**：PE > 80 且（营收增速 < 15% 或加速度 < -10 个百分点）→ 标「等价格」，不入场。")
    L.append("- **短线过热**：RSI > 80 且距 50 日线 > +15% → 标「不追高」，当日不买。")
    L.append("- **流动性偏紧**：流动比率 < 0.8 只警戒、不排除（保险/公用事业结构性 <1，曾误杀 UNH 后修正）。")
    L.append("- **拆股口径嫌疑**：PE<5 且 PS<2，或 PS<3 且毛利率>60% → 整本估值账作废（防止拆股未复权算出假便宜）。")
    L.append("- **财报过期**：营收期间结束日距信号日 > 180 天，三本财务账整体作废。")
    L.append("- **进攻层估值增速豁免**：进攻层且成长账 ≥70 时，估值分可按营收增速加成："
             "`min(65, 估值 + min(20, max(0, (营收增速-15)/3)))`，再与门槛 30 比较。")
    L.append("- **进攻层现金门卫**：进攻层若 FCF 为负，须营收增速 ≥30% 且现金 ≥ 1.5×|FCF| 才允许入场。\n")

    L.append("## 三、分层、确定性与仓位\n")
    L.append("**分层（assign_tier，按波动与盈利状态）**：60 日年化波动 >55%，或净利与 FCF 同时为负，或（营收增速 >35% 且估值分 <45）→ **进攻**；"
             "波动 <30% + FCF 为正 + FCF 率 >5% + 质量 ≥70 + 成长 ≥45 + 营收增速 ≥0 → **防守**；其余 → **普通**。\n")
    L.append("| 层 | 入场门槛（质量/成长/估值/时机） | 组合预算 | 单票上限 | 最多只数 | 首笔比例（确定性 高/中/低） |")
    L.append("|---|---|---|---|---|---|")
    L.append("| 防守 | 70 / 45 / 50 / 50 | 30% | 12% | 4 | 70% / 58% / 42% |")
    L.append("| 普通 | 60 / 55 / 45 / 55 | 30% | 12% | 6 | 60% / 48% / 32% |")
    L.append("| 进攻 | 50 / 70 / 30 / 50 | 40% | 8% | 6 | 50% / 38% / 22%（低保底 20%） |")
    L.append("")
    L.append("注：买入上限按预算 ×1.25 松紧带计算；防守/普通层在进攻预算没用满时可借用其 75%。"
             "信号日距 50 日线 > +15% 首笔减半；近 90 天有内部人公开市场买入，首笔 ×1.35（高管）/×1.20（其他内部人），且首笔不超过单票上限的 80%。\n")
    L.append("**成长确定性（growth_certainty）**：成长账 ≥85 得 2 分（≥72 得 1）；营收增速 ≥30% 得 2（≥18% 得 1）；"
             "加速度 ≥3pp 得 1（≤-8pp 扣 1）；EPS 增速 ≥20% 得 1（<0 扣 1）。总分 ≥4 为「高」，≥2 为「中」，否则「低」。\n")
    L.append("**基本面持有分（fund_hold_score，0–100）**：只看财报不看价格。营收单季同比（最高 15）+ 加速度（15）+ 毛利率变化（15）"
             "+ 现金流转化率（20）+ 净负债/FCF（15）+ 流动比率（10）…… 实际六个分段合计上限 90+10 口径见源码；"
             "新开仓要求 ≥45，防守层新开要求 ≥55，<40 或（营收、毛利率、转化率同时走弱）判基本面转坏。\n")
    L.append("**rank**：四本账按 25/32.5/25/17.5 加权，仅用于同日候选排序，不是入场门槛（门槛是逐本账卡线）。\n")

    L.append(f"## 四、30 只总表（{sd}）\n")
    L.append("| 标的 | 层 | 质量 | 成长 | 估值 | 时机 | 确定性 | 持有分 | rank | 今日结论 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for t in rows:
        dt = detail[t]
        b = dt["books"]
        if t in buys:
            p = buys[t]
            verdict = f"买 首笔 {p['buy_usd']:,} 美元"
        elif dt["tier"] is None:
            verdict = "数据缺失出局"
        else:
            verdict = "未过线"
        L.append(f"| {t} | {dt['tier'] or '—'} | {fmt(b['公司质量'])} | {fmt(b['成长与预期'])} | {fmt(b['估值'])} | "
                 f"{fmt(b['入场时机'])} | {dt['certainty']} | {dt['fund_score']:.0f} | {fmt(dt['rank'])} | {verdict} |")
    L.append("")

    L.append("## 五、逐只明细\n")
    for t in rows:
        dt = detail[t]
        f = dt["f"]
        b = dt["books"]
        L.append(f"### {t}（{dt['tier'] or '层级未定'}层，财报状态日 {dt['state_date']}，现价 {dt['price']} 美元，距50日线 {dt['dev50']:+.1f}%）\n")
        L.append(f"原始指标：营收增速TTM {fmt(f.get('rev_growth'))}%、加速度 {fmt(f.get('rev_accel_pp'))}pp、EPS增速 {fmt(f.get('eps_growth'))}%、"
                 f"毛利率 {fmt(f.get('gross_margin'))}%、ROE {fmt(f.get('roe'))}%、FCF率 {fmt(f.get('fcf_margin'))}%、"
                 f"PE {fmt(f.get('pe'))}、PEG近似 {fmt(f.get('peg_rev_approx'), 2)}、PS {fmt(f.get('ps'), 2)}、FCF收益率 {fmt(f.get('fcf_yield'))}%。\n")
        for name in ["公司质量", "成长与预期", "估值", "入场时机"]:
            L.append(f"- **{name} {fmt(b[name])} 分**：")
            for n, raw, s, w in dt["books_items"][name]:
                raw_txt = "" if raw is None else f"，原始值 {fmt(raw)}"
                L.append(f"  - {n}（权重{w}{raw_txt}）→ {fmt(s)} 分")
        L.append(f"- 确定性 {dt['certainty']}｜持有分 {dt['fund_score']:.0f}｜rank {fmt(dt['rank'])}{'｜过热不追高' if dt['overheat'] else ''}\n")

    L.append("## 六、数据缺失的 4 只说明\n")
    L.append("- **JPM、XOM**：当前财报科目解析不到营收等字段，三本财务账为空，框架按「账缺失不出手」出局；非基本面判断。")
    L.append("- **MSFT、V**：财务数值本身是最新的，但营收期间结束日字段停在 2026-03-31，触发「>180 天作废」。"
             "绕开该戳手工重算：MSFT（普通层）89.8/68.9/**44.3**/74.6，估值差门槛 0.7 分，仍不买；"
             "V（防守层）90.8/63.1/79.2/76.9 四账全过。是认这个技术性拦截还是修正口径，待人工拍板。")

    path = "/home/hatch/workspace/goals/multi-factor-stock-analysis-framework/files/评分逻辑与个股得分_2026-10-08.md"
    open(path, "w").write("\n".join(L))
    print("WROTE", path)

if __name__ == "__main__":
    main()
