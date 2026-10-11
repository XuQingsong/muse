"""期权市场级情绪（免费层）：
1) VIX 全历史（CBOE 官方 CDN）-> options_vix_history.csv
   输出最新值、近1年/5年分位、状态（平静/正常/紧张/恐慌，阈值为经验值未验证）。
2) SPY+QQQ 期权链 Put/Call 成交量比：Nasdaq 公开接口只有当日快照、无历史，
   逐次运行向前积累 -> options_pc_forward.csv（只能前瞻，不能回测，明说）。
"""
import json, subprocess, sys
import pandas as pd
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

def fetch_l(url):
    return subprocess.run(["curl", "-sL", "--max-time", "40", "-A", UA, url],
                          capture_output=True, text=True).stdout

raw = json.loads(fetch_l("https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json"))
vix = pd.DataFrame(raw["data"])[["date", "close"]].rename(columns={"close": "vix"})
vix["vix"] = vix["vix"].astype(float)
vix.to_csv("options_vix_history.csv", index=False)
last = float(vix["vix"].iloc[-1]); last_date = vix["date"].iloc[-1]
def pct(s, x): return float((s <= x).mean() * 100)
p1 = pct(vix["vix"].tail(252), last); p5 = pct(vix["vix"].tail(1250), last)
regime = "平静" if last < 14 else "正常" if last < 20 else "紧张" if last < 30 else "恐慌"
print(f"VIX {last:.2f}（{last_date}）近1年分位 {p1:.0f}% 近5年分位 {p5:.0f}% 状态：{regime}")

def pc_ratio(sym, ac):
    d = json.loads(fetch(f"https://api.nasdaq.com/api/quote/{sym}/option-chain?assetclass={ac}&limit=9999", ua=UA))
    rows = ((d.get("data") or {}).get("table") or {}).get("rows") or []
    def num(x):
        try: return float(str(x).replace(",", ""))
        except (TypeError, ValueError): return 0.0
    cv = sum(num(r.get("c_Volume")) for r in rows if r.get("strike"))
    pv = sum(num(r.get("p_Volume")) for r in rows if r.get("strike"))
    coi = sum(num(r.get("c_Openinterest")) for r in rows if r.get("strike"))
    poi = sum(num(r.get("p_Openinterest")) for r in rows if r.get("strike"))
    return cv, pv, coi, poi

today = pd.Timestamp.now(tz="Asia/Shanghai").strftime("%Y-%m-%d")
rec = {"date": today}
for sym, ac in [("SPY", "etf"), ("QQQ", "etf")]:
    cv, pv, coi, poi = pc_ratio(sym, ac)
    rec[f"{sym}_pc_vol"] = round(pv / cv, 3) if cv else None
    rec[f"{sym}_pc_oi"] = round(poi / coi, 3) if coi else None
    print(f"{sym}: 成交量P/C {rec[f'{sym}_pc_vol']} 持仓P/C {rec[f'{sym}_pc_oi']} (call量{cv:,.0f} put量{pv:,.0f})")

try:
    old = pd.read_csv("options_pc_forward.csv")
    old = old[old["date"] != today]
    pd.concat([old, pd.DataFrame([rec])]).to_csv("options_pc_forward.csv", index=False)
except FileNotFoundError:
    pd.DataFrame([rec]).to_csv("options_pc_forward.csv", index=False)
print("已更新 options_pc_forward.csv（前瞻积累，无历史不可回测）")
