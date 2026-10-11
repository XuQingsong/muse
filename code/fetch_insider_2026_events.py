"""补抓事件级内部人交易 2025-12-01 ~ 2026-10-07（供 v17 纸面模拟 PIT 使用）。
2026 年汇总文件只有聚合数，这里输出与 info_insider_hist.json 同结构的事件列表。
口径：filed 为可见日，只取公开市场买卖 P/S。
"""
import json, sys, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
LO, HI = "2025-12-01", "2026-10-08"

def all_filings(t, forms):
    cik = cik_of(t)
    sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ua=SEC_UA))
    chunks = [sub["filings"]["recent"]]
    for extra in sub["filings"].get("files", []):
        if extra["filingTo"] < LO:
            continue
        chunks.append(json.loads(fetch(f"https://data.sec.gov/submissions/{extra['name']}", ua=SEC_UA)))
    rows = []
    for rec in chunks:
        n = len(rec["form"])
        for i in range(n):
            if rec["form"][i] in forms and LO <= rec["filingDate"][i] < HI:
                rows.append({"form": rec["form"][i], "filed": rec["filingDate"][i],
                             "acc": rec["accessionNumber"][i].replace("-", ""),
                             "doc": rec["primaryDocument"][i]})
    return int(cik), rows

def parse_form4(xml_text):
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
        if code not in ("P", "S"):
            continue
        d = txn.findtext(".//transactionDate/value") or ""
        sh = txn.findtext(".//transactionShares/value")
        px = txn.findtext(".//transactionPricePerShare/value")
        try:
            sh = float(sh); px = float(px)
        except (TypeError, ValueError):
            continue
        out.append({"date": d, "code": code, "shares": sh, "price": px,
                    "owner": who, "officer": is_off, "director": is_dir})
    return out

insider = {}
for t in TICKS:
    cik, rows = all_filings(t, ("4", "4/A"))
    def job4(r):
        try:
            xml = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc']}/{r['doc'].split('/')[-1]}", ua=SEC_UA)
            time.sleep(0.05)
            return [{"filed": r["filed"], **e} for e in parse_form4(xml)]
        except Exception:
            return []
    ev = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(job4, rows):
            ev.extend(part)
    insider[t] = sorted(ev, key=lambda e: e["filed"])
    print(f"{t}: Form4 {len(rows)}份，P事件 {sum(1 for e in ev if e['code'] == 'P')}，S事件 {sum(1 for e in ev if e['code'] == 'S')}", flush=True)

with open("info_insider_events_2026.json", "w") as f:
    json.dump(insider, f, ensure_ascii=False, indent=1)
print("已写入 info_insider_events_2026.json")
