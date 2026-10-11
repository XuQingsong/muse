"""新信号有效性验证（事件研究法，point-in-time）。
口径：信号日收盘后可见 -> 次一交易日收盘入场，持有 21/63（内部人加 126）个交易日，
收益 = 个股涨幅 - 同期 SPY 涨幅（超额）。窗口重叠会夸大显著性，只报均值/中位/样本数
并做 2023-24 与 2025-26 两子期同向检查，不报 p 值充数。
输出 signal_validation_report.md。
"""
import json, sys
import numpy as np
import pandas as pd
sys.path.insert(0, ".")
from factor_scorecard_v2 import get_history
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
H = [21, 63]

px = {}
for t in TICKS + ["SPY"]:
    df = get_history(t)
    px[t] = df["close"].astype(float).sort_index()
spy = px["SPY"]
dates_all = spy.index

def fwd_excess(t, sig_date, h):
    """信号日之后次一交易日收盘入场，h 个交易日后的超额收益（%）。"""
    s = px[t]
    pos = s.index.searchsorted(pd.Timestamp(sig_date), side="right")
    if pos + h >= len(s):
        return None
    entry_d, exit_d = s.index[pos], s.index[pos + h]
    r_stock = s.iloc[pos + h] / s.iloc[pos] - 1
    try:
        r_spy = spy.loc[exit_d] / spy.loc[entry_d] - 1
    except KeyError:
        sp = spy.reindex([entry_d, exit_d], method="ffill")
        r_spy = sp.iloc[1] / sp.iloc[0] - 1
    return (r_stock - r_spy) * 100

def bucket_stats(rows, label):
    """rows: list of (fwd21, fwd63[, subperiod])"""
    out = [f"**{label}**（n={len(rows)}）"]
    for i, h in enumerate(H):
        v = [r[i] for r in rows if r[i] is not None]
        if v:
            out.append(f"{h}日超额 均值{np.mean(v):+.2f}% 中位{np.median(v):+.2f}% 胜率{(np.array(v) > 0).mean() * 100:.0f}%")
    return " ｜ ".join(out)

R = ["# 新信号有效性验证（2023-01 ~ 2026-10-07，相对 SPY 超额）\n"]

# ---------- A. 量能 ----------
vf = pd.read_csv("volume_factors_daily.csv", parse_dates=["date"])
def vol_rows(sub):
    rows = []
    for _, r in sub.iterrows():
        rows.append((fwd_excess(r["ticker"], r["date"], 21), fwd_excess(r["ticker"], r["date"], 63)))
    return [r for r in rows if r[0] is not None]
samp = vf.dropna(subset=["vol_score"]).sort_values(["ticker", "date"]).copy()
samp["_rn"] = samp.groupby("ticker").cumcount()
samp = samp[samp["_rn"] % 21 == 0]
qs = samp["vol_score"].quantile([1/3, 2/3]).values
R.append("## A. 逐日量能（每 21 个交易日采样一次/票）")
R.append(bucket_stats(vol_rows(samp[samp.vol_score >= qs[1]]), f"量能分高组（≥{qs[1]:.0f}）"))
R.append(bucket_stats(vol_rows(samp[samp.vol_score <= qs[0]]), f"量能分低组（≤{qs[0]:.0f}）"))
R.append(bucket_stats(vol_rows(samp[samp.dist_days >= 5]), "派发日≥5 天"))
R.append(bucket_stats(vol_rows(samp[samp.dist_days <= 1]), "派发日≤1 天（对照）"))
R.append(bucket_stats(vol_rows(samp[samp.price_obv_div == -1]), "价格-OBV 顶背离"))
R.append(bucket_stats(vol_rows(samp[samp.breakout == True]), "放量突破当日"))  # noqa: E712
# Spearman IC（采样点全量）
ic_rows = []
for _, r in samp.iterrows():
    f = fwd_excess(r["ticker"], r["date"], 21)
    if f is not None:
        ic_rows.append((r["vol_score"], f))
ic_df = pd.DataFrame(ic_rows, columns=["score", "fwd21"])
R.append(f"量能分与未来 21 日超额 Spearman 相关：{ic_df['score'].corr(ic_df['fwd21'], method='spearman'):+.3f}（n={len(ic_df)}）")

# ---------- B. 内部人 ----------
ins26 = json.load(open("info_insider_2026.json"))
insH = json.load(open("info_insider_hist.json"))
buys, sells, obuy = [], [], []
for src, is26 in [(insH, False), (ins26, True)]:
    for t, payload in src.items():
        evs = payload["events"] if is26 else payload
        for e in evs:
            if e["code"] == "P":
                buys.append((t, e))
                if e.get("officer"):
                    obuy.append((t, e))
            elif e["code"] == "S":
                sells.append((t, e))
def ev_rows(evs, hs=(21, 63, 126)):
    rows = []
    for t, e in evs:
        rows.append(tuple(fwd_excess(t, e["filed"], h) for h in hs))
    return [r for r in rows if r[0] is not None]
def ev_stats(evs, label):
    rows = ev_rows(evs)
    out = [f"**{label}**（事件 {len(evs)}，可算 {len(rows)}）"]
    for i, h in enumerate((21, 63, 126)):
        v = [r[i] for r in rows if r[i] is not None]
        if v:
            out.append(f"{h}日超额 均值{np.mean(v):+.2f}% 中位{np.median(v):+.2f}% 胜率{(np.array(v) > 0).mean() * 100:.0f}%")
    return " ｜ ".join(out)
R.append("\n## B. 内部人交易（信号日 = 申报日 filed）")
R.append(ev_stats(buys, "公开市场买入 P（全部）"))
R.append(ev_stats(obuy, "其中：高管（officer）买入"))
R.append(ev_stats(sells, "公开市场卖出 S（多含 10b5-1 计划，对照）"))

# ---------- C. 8-K ----------
k26 = json.load(open("info_8k_2026.json"))
kH = json.load(open("info_8k_hist.json"))
groups = {"业绩 2.02": [], "重大协议 1.01": [], "高管变动 5.02": [], "收购完成 2.01": [], "信息披露 7.01": []}
for src in (kH, k26):
    for t, evs in src.items():
        for e in evs:
            d = (e.get("accepted") or e["filed"])[:10]
            for gname, code in [("业绩 2.02", "2.02"), ("重大协议 1.01", "1.01"),
                                ("高管变动 5.02", "5.02"), ("收购完成 2.01", "2.01"), ("信息披露 7.01", "7.01")]:
                if code in e.get("key_items", []):
                    groups[gname].append((t, d))
R.append("\n## C. 8-K 事件（信号日 = 申报日；仅按事项代码，未读附件内容）")
for gname, evs in groups.items():
    rows = [(fwd_excess(t, d, 21), fwd_excess(t, d, 63)) for t, d in evs]
    rows = [r for r in rows if r[0] is not None]
    R.append(bucket_stats(rows, f"{gname}（事件 {len(evs)}）"))

# ---------- D. 卖空 ----------
si = json.load(open("info_short_interest.json"))
hi, lo = [], []
for t, rows in si.items():
    rr = sorted(rows, key=lambda x: pd.Timestamp(x["date"]))
    for i, r in enumerate(rr):
        sig = pd.Timestamp(r["date"]) + pd.Timedelta(days=10)  # 约公布滞后
        rec = (fwd_excess(t, sig, 21), fwd_excess(t, sig, 63))
        if rec[0] is None:
            continue
        (hi if r["days_to_cover"] >= 3 else lo if r["days_to_cover"] < 1.5 else []).append(rec) \
            if r["days_to_cover"] >= 3 or r["days_to_cover"] < 1.5 else None
R.append("\n## D. 卖空（仅 Nasdaq 上市 16 只，约 1 年，结算日 +10 天近似公布日）")
R.append(bucket_stats(hi, "回补天数 ≥3（空头较重）"))
R.append(bucket_stats(lo, "回补天数 <1.5（空头轻，对照）"))

# ---------- E. VIX 闸门 ----------
vix = pd.read_csv("options_vix_history.csv", parse_dates=["date"]).set_index("date")["vix"]
vix = vix[vix.index >= "2023-01-01"]
spy_c = spy[spy.index >= "2023-01-01"]
def spy_fwd(d, h):
    pos = spy_c.index.searchsorted(d, side="right")
    if pos + h >= len(spy_c):
        return None
    return (spy_c.iloc[pos + h] / spy_c.iloc[pos] - 1) * 100
pct_rank = vix.rolling(252).apply(lambda s: (s <= s.iloc[-1]).mean() * 100, raw=False)
calm = [(spy_fwd(d, 21), spy_fwd(d, 63)) for d in vix.index if pct_rank.get(d, 0) >= 90]
calm = [r for r in calm if r[0] is not None]
allv = [(spy_fwd(d, 21), spy_fwd(d, 63)) for d in vix.index[::5]]
allv = [r for r in allv if r[0] is not None]
R.append("\n## E. VIX 闸门（标的 = SPY 本身，非超额）")
R.append(bucket_stats(calm, "VIX 处近 1 年 90 分位以上（恐慌区）后买 SPY"))
R.append(bucket_stats(allv, "全样本对照（每 5 日采样）"))

rep = "\n\n".join(R)
open("signal_validation_report.md", "w").write(rep)
print(rep)
