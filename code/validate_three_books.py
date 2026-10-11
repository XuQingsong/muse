"""PIT 三本账验证：用 financial_facts（逐条带 filed 日期）回算 2019–2026
每个月末的公司质量/成长/估值三本账分数（评分公式与 framework v3 完全一致，
interp 与 weighted_book 直接 import），再看分数与之后 21/63 个交易日相对
SPY 超额的关系。时点纪律：打分的财报只取当日已申报（filed<=D）的最新版本。

结果落 pit_book_scores 表（含原始指标 jsonb），过程打印统计。
"""
import json
import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, ".")
import db
from factor_framework_v3 import interp, weighted_book
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
FLOW_Q = ("revenue", "gross_profit", "net_income", "eps_diluted")
CF = ("ocf", "capex")

conn = db.get_conn()
facts = pd.read_sql("SELECT * FROM financial_facts", conn)
facts["filed"] = pd.to_datetime(facts["filed"])
facts["period_start"] = pd.to_datetime(facts["period_start"])
facts["period_end"] = pd.to_datetime(facts["period_end"])
facts["val"] = facts["val"].astype(float)

px = pd.read_sql("SELECT ticker, date, adj_close, source FROM prices_daily", conn)
px["date"] = pd.to_datetime(px["date"])
# SPY 有两套来源（ETF 数据集 1999–2025 + yfinance），同日优先 yfinance 口径
px["pref"] = (px["source"] != "yfinance").astype(int)
px = (px.sort_values("pref").drop_duplicates(["ticker", "date"], keep="first"))
PRICE = {t: g.set_index("date")["adj_close"].sort_index()
         for t, g in px.groupby("ticker")}
SPY = PRICE["SPY"]
CAL = SPY.index  # 交易日历

# ---------------- as-of 序列构建 ----------------

def _pick_versions(df, D):
    """filed<=D 内，每个 period_end 取 filed 最晚的一条。"""
    d = df[df["filed"] <= D]
    if d.empty:
        return d
    d = d.sort_values(["period_end", "filed", "accession"])
    return d.groupby("period_end").tail(1)


def quarter_series(facts_t, concept, D):
    """干净季度流量（报表科目）：各备选 tag 独立成序，取最新期末的那个 tag。"""
    sub = facts_t[facts_t["concept"] == concept]
    best = None
    for tag, g in sub.groupby("tag"):
        days = (g["period_end"] - g["period_start"]).dt.days
        g = g[(days >= 75) & (days <= 110)]
        v = _pick_versions(g, D)
        if len(v) >= 4:
            s = v.set_index("period_end")["val"].sort_index()
            if best is None or s.index[-1] > best.index[-1]:
                best = s
    return best


def cf_series(facts_t, concept, D):
    """现金流科目是累计口径（Q1/半年/前三季/全年），按同一财年起点去累计。"""
    sub = facts_t[facts_t["concept"] == concept]
    best = None
    for tag, g in sub.groupby("tag"):
        days = (g["period_end"] - g["period_start"]).dt.days
        g = g[((days >= 75) & (days <= 110)) | ((days >= 160) & (days <= 200))
              | ((days >= 250) & (days <= 290)) | ((days >= 330) & (days <= 380))]
        v = _pick_versions(g, D)
        if len(v) < 4:
            continue
        qs = {}
        for start, grp in v.groupby("period_start"):
            grp = grp.sort_values("period_end")
            prev = 0.0
            for _, r in grp.iterrows():
                qs[r["period_end"]] = r["val"] - prev
                prev = r["val"]
        s = pd.Series(qs).sort_index()
        if len(s) >= 4 and (best is None or s.index[-1] > best.index[-1]):
            best = s
    return best


def instant_val(facts_t, concepts, D, before=None):
    sub = facts_t[(facts_t["concept"].isin(concepts))
                  & (facts_t["period_start"] == facts_t["period_end"])]
    v = _pick_versions(sub, D)
    if v.empty:
        return None
    if before is not None:
        v = v[v["period_end"] <= before + pd.Timedelta(days=45)]
        if v.empty:
            return None
    return float(v.sort_values("period_end")["val"].iloc[-1])


def series_growth(s, periods=4):
    if s is None or len(s) < periods * 2:
        return None
    cur, prev = s.iloc[-periods:].sum(), s.iloc[-periods * 2:-periods].sum()
    return (cur / prev - 1) * 100 if prev else None


def quarter_yoy(s):
    if s is None or len(s) < 8:
        return None, None
    vals = []
    for back in (0, 1):
        cur = s.iloc[-1 - back]
        prior = s.iloc[-5 - back] if len(s) >= 5 + back else None
        vals.append((cur / prior - 1) * 100 if prior not in (None, 0) else None)
    return vals[0], vals[1]


def fundamentals_asof(facts_t, t, D, price):
    rev = quarter_series(facts_t, "revenue", D)
    gp = quarter_series(facts_t, "gross_profit", D)
    ni = quarter_series(facts_t, "net_income", D)
    eps = quarter_series(facts_t, "eps_diluted", D)
    ocf = cf_series(facts_t, "ocf", D)
    capex = cf_series(facts_t, "capex", D)
    f = {}
    if rev is not None and len(rev) >= 4:
        f["rev_ttm"] = float(rev.iloc[-4:].sum())
        f["rev_period"] = str(rev.index[-1].date())
    f["rev_growth"] = series_growth(rev)
    q_yoy, q_prev = quarter_yoy(rev)
    f["rev_accel_pp"] = (None if q_yoy is None or q_prev is None
                         else q_yoy - q_prev)
    if eps is not None and len(eps) >= 8:
        ec, ep = float(eps.iloc[-4:].sum()), float(eps.iloc[-8:-4].sum())
        f["eps_growth"] = (ec / ep - 1) * 100 if ec > 0 and ep > 0 else None
    else:
        f["eps_growth"] = None
    if gp is not None and rev is not None and len(gp) >= 4 and len(rev) >= 4:
        f["gross_margin"] = float(gp.iloc[-4:].sum() / rev.iloc[-4:].sum() * 100)
        if len(gp) >= 8 and len(rev) >= 8:
            f["gross_margin_change_pp"] = f["gross_margin"] - float(
                gp.iloc[-8:-4].sum() / rev.iloc[-8:-4].sum() * 100)
    if ni is not None and len(ni) >= 4:
        f["ni_ttm"] = float(ni.iloc[-4:].sum())
    if eps is not None and len(eps) >= 4:
        e = float(eps.iloc[-4:].sum())
        f["eps_ttm"] = e
        if e > 0:
            f["pe"] = price / e
    if ocf is not None and capex is not None and len(ocf) >= 4 and len(capex) >= 4:
        f["fcf_ttm"] = float(ocf.iloc[-4:].sum() - capex.iloc[-4:].sum())
    f["fcf_margin"] = (f["fcf_ttm"] / f["rev_ttm"] * 100
                       if "fcf_ttm" in f and f.get("rev_ttm") else None)
    f["fcf_conversion"] = (f["fcf_ttm"] / f["ni_ttm"]
                           if "fcf_ttm" in f and f.get("ni_ttm") else None)
    last_q_end = pd.Timestamp(f["rev_period"]) if "rev_period" in f else D
    eq = instant_val(facts_t, ["equity"], D, before=last_q_end)
    if "ni_ttm" in f and eq:
        f["roe"] = f["ni_ttm"] / eq * 100
    cash = instant_val(facts_t, ["cash"], D)
    dc = instant_val(facts_t, ["debt_cur"], D)
    dn = instant_val(facts_t, ["debt_noncur"], D)
    debt = sum(x for x in (dc, dn) if x is not None) if (dc or dn) else None
    if debt is not None:
        f["net_debt"] = debt - (cash or 0.0)
        if f.get("fcf_ttm", 0) and f["fcf_ttm"] > 0:
            f["net_debt_fcf"] = f["net_debt"] / f["fcf_ttm"]
    shares = instant_val(facts_t, ["shares_out", "shares_out_dei"], D)
    if shares and f.get("rev_ttm"):
        f["ps"] = price * shares / f["rev_ttm"]
    if shares and "fcf_ttm" in f:
        f["fcf_yield"] = f["fcf_ttm"] / (price * shares) * 100
    if f.get("pe") and f.get("rev_growth") and f["rev_growth"] > 0:
        f["peg_rev_approx"] = f["pe"] / f["rev_growth"]
    return f


def books(f):
    quality = weighted_book([
        ("毛利率", None, interp(f.get("gross_margin"), [(5, 5), (15, 30), (25, 50), (40, 75), (60, 95)]), 25, ""),
        ("ROE", None, interp(f.get("roe"), [(-10, 0), (0, 25), (5, 45), (15, 75), (25, 95)]), 25, ""),
        ("自由现金流率", None, interp(f.get("fcf_margin"), [(-10, 0), (0, 30), (5, 55), (10, 75), (20, 95)]), 25, ""),
        ("现金流/净利润", None, interp(f.get("fcf_conversion"), [(-0.5, 0), (0, 30), (0.5, 60), (1.0, 85), (1.5, 95)]), 15, ""),
        ("净负债/自由现金流", None, interp(f.get("net_debt_fcf"), [(-1, 100), (0, 95), (1, 85), (3, 60), (5, 30), (8, 5)]), 10, ""),
    ])[0]
    growth = weighted_book([
        ("营收增速TTM", None, interp(f.get("rev_growth"), [(-15, 0), (-5, 15), (0, 35), (5, 50), (10, 65), (20, 82), (35, 95), (60, 100)]), 40, ""),
        ("营收增速加速度", None, interp(f.get("rev_accel_pp"), [(-20, 5), (-10, 25), (-3, 45), (0, 55), (5, 70), (15, 90)]), 25, ""),
        ("EPS增速TTM", None, interp(f.get("eps_growth"), [(-30, 0), (-10, 20), (0, 40), (10, 60), (25, 80), (50, 95)]), 25, ""),
        ("毛利率变化", None, interp(f.get("gross_margin_change_pp"), [(-5, 10), (-2, 35), (0, 55), (2, 75), (5, 95)]), 10, ""),
    ])[0]
    valuation = weighted_book([
        ("PE(TTM)", None, interp(f.get("pe"), [(8, 98), (12, 90), (20, 72), (30, 52), (50, 28), (80, 10), (120, 3)]), 35, ""),
        ("PEG近似", None, interp(f.get("peg_rev_approx"), [(0.5, 95), (1.0, 78), (1.5, 58), (2.5, 32), (4.0, 12)]), 25, ""),
        ("PS", None, interp(f.get("ps"), [(0.5, 98), (1, 90), (3, 72), (6, 50), (10, 28), (20, 8)]), 20, ""),
        ("FCF收益率", None, interp(f.get("fcf_yield"), [(-2, 0), (0, 12), (1, 32), (3, 58), (5, 78), (8, 95)]), 20, ""),
    ])[0]
    return quality, growth, valuation


def fwd_excess(t, D, h):
    s = PRICE[t]
    i = s.index.searchsorted(D, side="right")
    j = SPY.index.searchsorted(D, side="right")
    if i + h >= len(s) or j + h >= len(SPY):
        return None
    return ((s.iloc[i + h] / s.iloc[i] - 1)
            - (SPY.iloc[j + h] / SPY.iloc[j] - 1)) * 100


_cal = pd.DataFrame({"d": CAL})
month_ends = list(_cal.groupby([_cal["d"].dt.year, _cal["d"].dt.month])
                  ["d"].max())
month_ends = [d for d in month_ends
              if pd.Timestamp("2019-06-01") <= d <= pd.Timestamp("2026-09-30")]

rows = []
for t in TICKS:
    ft = facts[facts["ticker"] == t]
    for D in month_ends:
        price = PRICE[t][PRICE[t].index <= D]
        if price.empty:
            continue
        f = fundamentals_asof(ft, t, D, float(price.iloc[-1]))
        q, g, v = books(f)
        rows.append({"ticker": t, "asof": D.date(), "quality": q,
                     "growth": g, "valuation": v, "raw": f,
                     "ex21": fwd_excess(t, D, 21), "ex63": fwd_excess(t, D, 63)})
ev = pd.DataFrame(rows)
print(f"截面样本：{len(ev)} 个（{ev['ticker'].nunique()} 只 × "
      f"{ev['asof'].nunique()} 个月末）")

cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS pit_book_scores (
    ticker text NOT NULL, asof_date date NOT NULL,
    quality numeric, growth numeric, valuation numeric,
    raw jsonb,
    PRIMARY KEY (ticker, asof_date))""")
ins = [(r.ticker, r.asof, r.quality, r.growth, r.valuation,
        json.dumps({k: v for k, v in r.raw.items()
                    if isinstance(v, (int, float, str))}))
       for r in ev.itertuples()]
cur.executemany("INSERT INTO pit_book_scores VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING", ins)
conn.commit()
print("pit_book_scores 已入库")


def ic(df, col, hcol):
    d = df.dropna(subset=[col, hcol])
    if len(d) < 30:
        return None
    return spearmanr(d[col], d[hcol]).statistic, len(d)


print("\n=== 三本账 vs 未来超额（Spearman IC） ===")
ev["sub"] = np.where(pd.to_datetime(ev["asof"]) < "2023-01-01",
                     "2019-2022", "2023-2026")
for book in ("quality", "growth", "valuation"):
    for hcol in ("ex21", "ex63"):
        all_ = ic(ev, book, hcol)
        s1 = ic(ev[ev["sub"] == "2019-2022"], book, hcol)
        s2 = ic(ev[ev["sub"] == "2023-2026"], book, hcol)
        fmt = lambda x: f"{x[0]:+.3f}(n={x[1]})" if x else "样本不足"
        print(f"{book} {hcol}: 全期 {fmt(all_)} | 2019-22 {fmt(s1)} | "
              f"2023-26 {fmt(s2)}")

print("\n=== 单项指标 IC（63日） ===")
METRICS = ["gross_margin", "roe", "fcf_margin", "fcf_conversion",
           "rev_growth", "rev_accel_pp", "eps_growth",
           "gross_margin_change_pp", "pe", "ps", "fcf_yield",
           "peg_rev_approx"]
ev_raw = pd.DataFrame([r.raw for r in ev.itertuples()])
ev_raw["ex63"] = ev["ex63"].values
ev_raw["sub"] = ev["sub"].values
for m in METRICS:
    if m not in ev_raw:
        continue
    a = ic(ev_raw, m, "ex63")
    s1 = ic(ev_raw[ev_raw["sub"] == "2019-2022"], m, "ex63")
    s2 = ic(ev_raw[ev_raw["sub"] == "2023-2026"], m, "ex63")
    fmt = lambda x: f"{x[0]:+.3f}" if x else "—"
    print(f"{m}: 全期 {fmt(a)} | 2019-22 {fmt(s1)} | 2023-26 {fmt(s2)}")

print("\n=== 三分位组合（63日中位超额，当月截面内分组） ===")
for book in ("quality", "growth", "valuation"):
    d = ev.dropna(subset=[book, "ex63"]).copy()
    d["grp"] = d.groupby("asof")[book].transform(
        lambda s: pd.qcut(s, 3, labels=["低", "中", "高"])
        if s.notna().sum() >= 9 else None)
    g = d.groupby("grp", observed=True)["ex63"].agg(["median", "count"])
    print(f"{book}: 低分组中位 {g.loc['低', 'median']:+.2f}% (n={int(g.loc['低', 'count'])})；"
          f"高分组 {g.loc['高', 'median']:+.2f}% (n={int(g.loc['高', 'count'])})")

ev.drop(columns=["raw"]).to_csv("three_books_events.csv", index=False)
conn.close()
print("\n事件明细已写 three_books_events.csv")
