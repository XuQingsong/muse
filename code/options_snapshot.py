"""个股期权动态快照（CBOE 延迟报价，免费；约15分钟延迟）。
每只票：Put/Call 成交量比、持仓比、IV30（30天隐含波动率）、IV偏度
（近月 25Δ put IV − 25Δ call IV，看下行保护溢价）。
历史不可得，只能从今天起逐日积累 options_snapshot_daily.csv —— 明说：不可回测历史。
"""
import json, re, subprocess, sys
import pandas as pd
sys.path.insert(0, ".")
from paper_trade_2026 import TICKERS

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
SYMS = TICKERS + ["MU", "GOOGL", "SPY", "QQQ"]

def fetch_l(url):
    return subprocess.run(["curl", "-sL", "--max-time", "40", "-A", UA, url],
                          capture_output=True, text=True).stdout

def parse(sym):
    m = re.match(r"([A-Z0-9]+?)(\d{6})([CP])(\d{8})", sym)
    if not m:
        return None
    return m.group(2), m.group(3), int(m.group(4)) / 1000

def snap(t):
    d = json.loads(fetch_l(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{t}.json"))["data"]
    rows = [(p, o) for o in d["options"] if (p := parse(o["option"])) is not None]
    cv = sum(o["volume"] for p, o in rows if p[1] == "C")
    pv = sum(o["volume"] for p, o in rows if p[1] == "P")
    coi = sum(o["open_interest"] for p, o in rows if p[1] == "C")
    poi = sum(o["open_interest"] for p, o in rows if p[1] == "P")
    exps = sorted({p[0] for p, _ in rows})
    near_exp = exps[1] if len(exps) > 1 else exps[0]
    def iv_at(delta_tgt, typ):
        cand = [(abs(abs(o["delta"]) - delta_tgt), o["iv"]) for p, o in rows
                if p[0] == near_exp and p[1] == typ and o["iv"] > 0]
        return min(cand)[1] if cand else None
    put_iv, call_iv = iv_at(0.25, "P"), iv_at(0.25, "C")
    return {"ticker": t, "price": d.get("current_price"), "iv30": d.get("iv30"),
            "iv30_chg_pct": d.get("iv30_change_percent"),
            "pc_vol": round(pv / cv, 3) if cv else None,
            "pc_oi": round(poi / coi, 3) if coi else None,
            "call_vol": cv, "put_vol": pv,
            "skew25": round((put_iv - call_iv) * 100, 2) if put_iv and call_iv else None,
            "asof": d.get("last_trade_time")}

today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
out = []
for t in SYMS:
    try:
        s = snap(t); s["date"] = today; out.append(s)
        print(f"{t}: 价{s['price']} IV30 {s['iv30']} P/C量比{s['pc_vol']} 持仓比{s['pc_oi']} 偏度{s['skew25']}", flush=True)
    except Exception as e:
        print(f"{t}: 失败 {type(e).__name__} {e}", flush=True)

new = pd.DataFrame(out)
try:
    old = pd.read_csv("options_snapshot_daily.csv")
    old = old[old["date"] != today]
    pd.concat([old, new]).to_csv("options_snapshot_daily.csv", index=False)
except FileNotFoundError:
    new.to_csv("options_snapshot_daily.csv", index=False)
print("已更新 options_snapshot_daily.csv")
