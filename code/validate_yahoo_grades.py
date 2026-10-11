"""Yahoo 评级动作免费验证：升级/降级/首次覆盖后 21/63 日相对 SPY 超额。
数据：estimates_snapshot_2026-10-08.json 里的 upgrades_downgrades（Yahoo 口径，
19119 条，2011-2026）。这是 FMP grades 端点的免费替代，先验"评级动作"这个
因子值不值得进系统。口径与 validate_signals.py 一致：事件日后第一个交易日
收盘入场。注意：同日多家机构常同向行动（财报后扎堆），事件不完全独立。
"""
import json, sys
import numpy as np
import pandas as pd
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

snap = json.load(open("estimates_snapshot_2026-10-08.json"))
groups = {}
for t, o in snap.items():
    for a in o["grade_actions"]:
        act = a.get("action")
        if act not in ("up", "down", "init"):
            continue
        d = a["date"]
        for h in (21, 63):
            v = fwd_excess(t, d, h)
            if v is not None:
                groups.setdefault((act, h), []).append(v)
                sub = "2011-2022" if d < "2023-01-01" else "2023-2026"
                groups.setdefault((act, h, sub), []).append(v)

print("=== Yahoo 评级动作：事件后相对SPY超额(%) ===")
for act in ("up", "down", "init"):
    for h in (21, 63):
        xs = groups.get((act, h), [])
        if not xs:
            continue
        a = np.array(xs)
        print(f"{act} {h}日 n={len(a)} 中位{np.median(a):+.2f}% 均值{a.mean():+.2f}% 胜率{(a > 0).mean() * 100:.0f}%")
    for sub in ("2011-2022", "2023-2026"):
        xs = groups.get((act, 63, sub), [])
        if xs:
            a = np.array(xs)
            print(f"   {act} 63日[{sub}] n={len(a)} 中位{np.median(a):+.2f}% 胜率{(a > 0).mean() * 100:.0f}%")
