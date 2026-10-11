"""yfinance 预期快照 v1（2026-10-08）：32 只的 EPS 预期 90 天修正、修订家数、
目标价、最近财报超预期。自存为第一份 PIT 快照，此后逐日积累。
注意：eps_trend 的 90 天前值是"今日回望"，只能算现况读数，不能回填历史验证。
输出 estimates_snapshot_2026-10-08.json。
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
import yfinance as yf
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]

def grab(t):
    tk = yf.Ticker(t)
    out = {"t": t}
    try:
        et = tk.eps_trend
        d = {}
        for period in ("0q", "+1q", "0y", "+1y"):
            if period in et.index:
                r = et.loc[period]
                cur, ago = float(r["current"]), float(r["90daysAgo"])
                d[period] = {"cur": cur, "ago90": ago,
                             "rev90_pct": (cur / ago - 1) * 100 if ago else None}
        out["eps_trend"] = d
    except Exception:
        out["eps_trend"] = {}
    try:
        er = tk.eps_revisions
        d = {}
        for period in ("0q", "0y"):
            if period in er.index:
                r = er.loc[period]
                d[period] = {"up30": int(r["upLast30days"]), "down30": int(r["downLast30days"]),
                             "up7": int(r["upLast7days"]), "down7": int(r["downLast7Days"])}
        out["eps_revisions"] = d
    except Exception:
        out["eps_revisions"] = {}
    try:
        pt = tk.analyst_price_targets
        out["price_target"] = {"current": pt.get("current"), "mean": pt.get("mean"),
                               "median": pt.get("median"), "high": pt.get("high"), "low": pt.get("low")}
    except Exception:
        out["price_target"] = {}
    try:
        eh = tk.earnings_history
        rows = []
        for q, r in eh.iterrows():
            rows.append({"quarter": str(q)[:10], "eps_actual": float(r["epsActual"]),
                         "eps_est": float(r["epsEstimate"]), "surprise_pct": float(r["surprisePercent"]) * 100})
        out["earnings_hist"] = rows
    except Exception:
        out["earnings_hist"] = []
    try:
        ud = tk.upgrades_downgrades
        acts = []
        for dt, r in ud.iterrows():
            acts.append({"date": str(dt)[:10], "firm": r.get("Firm"), "from": r.get("FromGrade"),
                         "to": r.get("ToGrade"), "action": r.get("Action")})
        out["grade_actions"] = acts
    except Exception:
        out["grade_actions"] = []
    return out

res = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for out in ex.map(grab, TICKS):
        res[out["t"]] = out
        print(out["t"], "完成", flush=True)
        time.sleep(0.2)

with open("estimates_snapshot_2026-10-08.json", "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=1)

print("\n=== 现况：FY 预期 90 天修正 / 近30天净修订 / 目标价空间 / 最近一季超预期 ===")
for t in TICKS:
    o = res[t]
    tr = o["eps_trend"].get("0y", {})
    rv = o["eps_revisions"].get("0y", {})
    pt = o["price_target"]
    eh = o["earnings_hist"]
    print(f"{t:5s} FY修正90d {tr.get('rev90_pct') if tr.get('rev90_pct') is not None else float('nan'):+6.2f}%  "
          f"净修订30d {rv.get('up30', 0) - rv.get('down30', 0):+3d}  "
          f"目标价/现价 {((pt.get('mean') or 0) / (pt.get('current') or 1) - 1) * 100 if pt.get('mean') and pt.get('current') else float('nan'):+6.1f}%  "
          f"最近季超预期 {eh[-1]['surprise_pct'] if eh else float('nan'):+5.1f}%  评级动作{len(o['grade_actions'])}条")
