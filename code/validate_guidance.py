"""指引信号事件验证（独立版，价格口径与 validate_signals.py 一致）：
8-K 附件解析出的指引方向（上调/下调/维持/首次给出）之后 21/63 个交易日
相对 SPY 超额（%）。口径：事件日（accepted）后第一个交易日收盘入场。
"""
import json, sys
import numpy as np
import pandas as pd
from collections import defaultdict
sys.path.insert(0, ".")
from factor_scorecard_v2 import get_history
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
px = {}
for t in TICKS + ["SPY"]:
    df = get_history(t)
    px[t] = df["close"].astype(float).sort_index()
spy = px["SPY"]

def fwd_excess(t, sig_date, h):
    if t not in px:
        return None
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

ex = json.load(open("info_8k_exhibits.json"))
groups = defaultdict(list)
for t, evs in ex.items():
    for e in evs:
        if not e["ex99_found"]:
            continue
        d = (e["accepted"] or "")[:10] or e["filed"]
        g = e["guidance"] or "none"
        for h in (21, 63):
            v = fwd_excess(t, d, h)
            if v is not None:
                groups[(g, h)].append(v)

lines = ["", "## 附：指引方向信号（8-K 附件解析后补验）", ""]
print("=== 指引方向：事件后相对SPY超额(%) ===")
for g in ("up", "down", "maintain", "withdraw", "initiate", "none"):
    for h in (21, 63):
        xs = groups.get((g, h), [])
        if not xs:
            print(f"{g} {h}日 n=0")
            continue
        a = np.array(xs)
        s = f"{g} {h}日 n={len(a)} 中位{np.median(a):+.2f}% 均值{a.mean():+.2f}% 胜率{(a > 0).mean() * 100:.0f}%"
        print(s)
        lines.append(f"- 指引{g}，{h}日：样本 {len(a)}，中位超额 {np.median(a):+.2f}%，均值 {a.mean():+.2f}%，胜率 {(a > 0).mean() * 100:.0f}%")

with open("signal_validation_report.md", "a") as f:
    f.write("\n".join(lines) + "\n")
print("已追加 signal_validation_report.md")
