"""免费信息面接入之一：
1) SEC EDGAR Form 4 内部人交易（2026-01-01 起，30 只票）
   只统计公开市场买入(P)/卖出(S)，赠予/行权/授予单列计数。
   信号日用 transactionDate，可见日用 filingDate（回测必须用 filingDate 防前视）。
2) Nasdaq 公开接口 short-interest 卖空数据（双周结算，全历史行原样存）。
输出：info_insider_2026.json、info_short_interest.json
"""
import json, sys, time
import xml.etree.ElementTree as ET
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
SINCE = "2026-01-01"

def parse_form4(xml_text):
    """返回 [(txn_date, code, shares, price, ad_code, owner_name, is_officer, is_director)]"""
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    owner = root.findtext(".//rptOwnerName") or ""
    rel = root.find(".//reportingOwnerRelationship")
    is_off = (rel is not None and (rel.findtext("isOfficer") or "").strip() in ("1", "true"))
    is_dir = (rel is not None and (rel.findtext("isDirector") or "").strip() in ("1", "true"))
    title = (rel.findtext("officerTitle") if rel is not None else "") or ""
    who = owner + (f"({title})" if title else "")
    for txn in root.iter("nonDerivativeTransaction"):
        code = txn.findtext(".//transactionCode") or ""
        d = txn.findtext(".//transactionDate/value") or ""
        sh = txn.findtext(".//transactionShares/value")
        px = txn.findtext(".//transactionPricePerShare/value")
        ad = txn.findtext(".//transactionAcquiredDisposedCode/value") or ""
        try:
            sh = float(sh); px = float(px)
        except (TypeError, ValueError):
            continue
        out.append((d, code, sh, px, ad, who, is_off, is_dir))
    return out

from concurrent.futures import ThreadPoolExecutor

def _fetch_one(args):
    cik_int, acc, doc, filed = args
    try:
        xml = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}", ua=SEC_UA)
        time.sleep(0.05)
        return filed, parse_form4(xml)
    except Exception:
        return filed, []

insider = {}
for t in TICKS:
    cik = cik_of(t)
    sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ua=SEC_UA))
    rec = sub["filings"]["recent"]
    idx = [i for i in range(len(rec["form"]))
           if rec["form"][i] in ("4", "4/A") and rec["filingDate"][i] >= SINCE]
    jobs = [(int(cik), rec["accessionNumber"][i].replace("-", ""),
             rec["primaryDocument"][i].split("/")[-1], rec["filingDate"][i]) for i in idx]
    ev = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for filed, txns in ex.map(_fetch_one, jobs):
            for (d, code, sh, px, ad, who, io, idr) in txns:
                if d >= SINCE:
                    ev.append({"date": d, "filed": filed, "code": code,
                               "shares": sh, "price": px, "ad": ad, "owner": who,
                               "officer": io, "director": idr})
    P = [e for e in ev if e["code"] == "P"]
    S = [e for e in ev if e["code"] == "S"]
    agg = {
        "filings": len(idx), "txn_total": len(ev),
        "buy_events": len(P), "buy_shares": sum(e["shares"] for e in P),
        "buy_value_usd": sum(e["shares"] * e["price"] for e in P),
        "buyers": sorted({e["owner"] for e in P}),
        "last_buy": max((e["date"] for e in P), default=None),
        "sell_events": len(S), "sell_shares": sum(e["shares"] for e in S),
        "sell_value_usd": sum(e["shares"] * e["price"] for e in S),
        "sellers": sorted({e["owner"] for e in S}),
        "last_sell": max((e["date"] for e in S), default=None),
        "other_codes": {c: sum(1 for e in ev if e["code"] == c)
                        for c in sorted({e["code"] for e in ev} - {"P", "S"})},
        "events": ev,
    }
    insider[t] = agg
    print(f"{t}: 申报{len(idx)} 买入{len(P)}笔/{agg['buy_shares']:,.0f}股 卖出{len(S)}笔/{agg['sell_shares']:,.0f}股")

with open("info_insider_2026.json", "w") as f:
    json.dump(insider, f, ensure_ascii=False, indent=1)

# ---- 卖空（Nasdaq 公开接口） ----
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
short = {}
for t in TICKS:
    try:
        d = json.loads(fetch(f"https://api.nasdaq.com/api/quote/{t}/short-interest?assetclass=stocks", ua=UA))
        rows = ((d.get("data") or {}).get("shortInterestTable") or {}).get("rows") or []
        short[t] = [{"date": r["settlementDate"],
                     "short_shares": int(str(r["interest"]).replace(",", "")),
                     "avg_daily_vol": int(str(r["avgDailyShareVolume"]).replace(",", "")),
                     "days_to_cover": r["daysToCover"]} for r in rows]
    except Exception as e:
        short[t] = []
        print(f"{t} short-interest 失败: {e}")
    time.sleep(0.15)
with open("info_short_interest.json", "w") as f:
    json.dump(short, f, ensure_ascii=False, indent=1)
print("卖空数据行数:", {t: len(v) for t, v in short.items() if v})
