"""每日预期快照（PIT 自存）：yfinance 32 只 -> PostgreSQL quant 库。
- estimates_trend：含 7/30/60/90 天前共识（当日回望版），每日存一行，
  日积月累后"修正"就能用自存序列算，不再依赖回望值。
- grade_actions：Yahoo 评级动作全量增量入库（PK 去重）。
运行：.venv/bin/python fetch_estimates_daily.py（cron 每日调用）
"""
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
import yfinance as yf

import db
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
TODAY = datetime.now(ZoneInfo("Asia/Shanghai")).date()


def grab(t):
    tk = yf.Ticker(t)
    out = {"t": t, "trend": {}, "revisions": {}, "pt": {}, "eh": [], "ga": []}
    try:
        et = tk.eps_trend
        for period in ("0q", "+1q", "0y", "+1y"):
            if period in et.index:
                r = et.loc[period]
                g = lambda k: float(r[k]) if k in r and r[k] == r[k] else None
                cur, ago90 = g("current"), g("90daysAgo")
                out["trend"][period] = {
                    "cur": cur, "ago7": g("7daysAgo"), "ago30": g("30daysAgo"),
                    "ago60": g("60daysAgo"), "ago90": ago90,
                    "rev90_pct": (cur / ago90 - 1) * 100
                    if cur and ago90 else None}
    except Exception:
        pass
    try:
        er = tk.eps_revisions
        for period in ("0q", "0y"):
            if period in er.index:
                r = er.loc[period]
                out["revisions"][period] = {
                    "up30": int(r["upLast30days"]),
                    "down30": int(r["downLast30days"]),
                    "up7": int(r["upLast7days"]),
                    "down7": int(r["downLast7Days"])}
    except Exception:
        pass
    try:
        pt = tk.analyst_price_targets
        out["pt"] = {"current": pt.get("current"), "mean": pt.get("mean"),
                     "median": pt.get("median"), "high": pt.get("high"),
                     "low": pt.get("low")}
    except Exception:
        pass
    try:
        for q, r in tk.earnings_history.iterrows():
            out["eh"].append((str(q)[:10], float(r["epsActual"]),
                              float(r["epsEstimate"]),
                              float(r["surprisePercent"]) * 100))
    except Exception:
        pass
    try:
        for dt, r in tk.upgrades_downgrades.iterrows():
            out["ga"].append((str(dt)[:10], r.get("Firm"), r.get("FromGrade"),
                              r.get("ToGrade"), r.get("Action")))
    except Exception:
        pass
    return out


res = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for out in ex.map(grab, TICKS):
        res[out["t"]] = out
        time.sleep(0.2)

conn = db.get_conn()
cur = conn.cursor()
n_tr = n_ga = 0
for t, o in res.items():
    cur.execute("INSERT INTO estimates_snapshot VALUES (%s,%s,'yfinance') "
                "ON CONFLICT DO NOTHING", (TODAY, t))
    for per, v in o["trend"].items():
        cur.execute("INSERT INTO estimates_trend VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (TODAY, t, per, v["cur"], v["ago7"], v["ago30"],
                     v["ago60"], v["ago90"], v["rev90_pct"]))
        n_tr += 1
    for per, v in o["revisions"].items():
        cur.execute("INSERT INTO estimates_revisions VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (TODAY, t, per, v["up30"], v["down30"], v["up7"], v["down7"]))
    pt = o["pt"]
    if pt.get("mean") is not None:
        cur.execute("INSERT INTO price_targets VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (TODAY, t, pt["current"], pt["mean"], pt["median"],
                     pt["high"], pt["low"]))
    for q, act, est, surp in o["eh"]:
        cur.execute("INSERT INTO earnings_history VALUES (%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING", (t, q, act, est, surp, TODAY))
    for d, firm, fg, tg, act in o["ga"]:
        cur.execute("INSERT INTO grade_actions VALUES (%s,%s,%s,%s,%s,%s,'yahoo') "
                    "ON CONFLICT DO NOTHING",
                    (t, d, firm or "", fg, tg, act or ""))
        n_ga += cur.rowcount
conn.commit()
print(f"{TODAY} 快照入库完成：{len(res)} 只，trend {n_tr} 行，"
      f"新增评级动作 {n_ga} 条")
conn.close()
