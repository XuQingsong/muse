"""探针：验证三个免费信息源在当前环境是否可达、返回什么。
1) SEC EDGAR submissions -> Form 4 内部人交易（MU 样例）
2) Nasdaq 公开接口 -> short-interest（卖空）
3) Nasdaq 公开接口 -> institutional-holdings（机构持仓汇总）
只做可用性验证，不落库。
"""
import json, sys, time
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA

T = "MU"

# ---- 1) EDGAR Form 4 ----
cik = cik_of(T)
sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ua=SEC_UA))
rec = sub["filings"]["recent"]
forms = rec["form"]
f4 = [(rec["filingDate"][i], rec["accessionNumber"][i], rec["primaryDocument"][i])
      for i in range(len(forms)) if forms[i] in ("4", "4/A")]
f4_2026 = [x for x in f4 if x[0] >= "2026-01-01"]
print(f"[EDGAR] {T} CIK={cik} 近期Form4总数={len(f4)} 2026年内={len(f4_2026)}")
if f4_2026:
    d, acc, doc = f4_2026[0]
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
    xml = fetch(url, ua=SEC_UA)
    print(f"[EDGAR] 样例 {d} {acc} XML长度={len(xml)} 含transactionCode={'<transactionCode>' in xml}")

# ---- 2/3) Nasdaq 公开接口 ----
for name, path in [("short-interest", f"quote/{T}/short-interest?assetclass=stocks"),
                    ("institutional-holdings", f"quote/{T}/institutional-holdings?assetclass=stocks"),
                    ("analyst-research", f"quote/{T}/analyst-research?assetclass=stocks")]:
    try:
        r = fetch(f"https://api.nasdaq.com/api/{path}",
                  ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")
        d = json.loads(r)
        data = d.get("data")
        print(f"[Nasdaq {name}] status={d.get('status')} data_keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
        if isinstance(data, dict):
            s = json.dumps(data)[:600]
            print("   样例:", s)
    except Exception as e:
        print(f"[Nasdaq {name}] 失败: {type(e).__name__} {e}")
    time.sleep(0.3)
