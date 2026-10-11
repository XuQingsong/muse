"""验证样本补抓：2023-01-01 ~ 2025-12-31 的 Form 4 买卖(P/S)与 8-K 事项。
时点口径：Form 4 用 filed 日期、8-K 用 acceptanceDateTime（均为当时可见时点）。
输出 info_insider_hist.json、info_8k_hist.json。含 SEC 申报分页（filings.files）。
"""
import json, re, sys, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

KEY_ITEMS = {"1.01", "1.02", "2.01", "2.02", "2.05", "2.06", "4.02", "5.01", "5.02", "7.01", "8.01"}
ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})", re.I)

def strip_html(txt):
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt)

TICKS = TICKERS + ["MU", "GOOGL"]
LO, HI = "2023-01-01", "2026-01-01"

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
                             "doc": rec["primaryDocument"][i],
                             "accepted": (rec.get("acceptanceDateTime") or [""] * n)[i]})
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

insider, eightk = {}, {}
for t in TICKS:
    cik, rows = all_filings(t, ("4", "4/A", "8-K", "8-K/A"))
    f4 = [r for r in rows if r["form"].startswith("4")]
    k8 = [r for r in rows if r["form"].startswith("8")]
    def job4(r):
        try:
            xml = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc']}/{r['doc'].split('/')[-1]}", ua=SEC_UA)
            time.sleep(0.05)
            return [{"filed": r["filed"], **e} for e in parse_form4(xml)]
        except Exception:
            return []
    def job8(r):
        try:
            raw = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc']}/{r['doc']}", ua=SEC_UA)
            time.sleep(0.05)
            text = strip_html(raw) if "<" in raw[:2000] else raw
            items = sorted({m.group(1) for m in ITEM_RE.finditer(text)})
            return {"filed": r["filed"], "accepted": r["accepted"], "items": items,
                    "key_items": [i for i in items if i in KEY_ITEMS]}
        except Exception:
            return {"filed": r["filed"], "accepted": r["accepted"], "items": [], "key_items": []}
    ev, ev8 = [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for part in ex.map(job4, f4):
            ev.extend(part)
        for e8 in ex.map(job8, k8):
            ev8.append(e8)
    insider[t] = ev
    eightk[t] = sorted(ev8, key=lambda e: e["accepted"] or e["filed"])
    nb = sum(1 for e in ev if e["code"] == "P"); ns = sum(1 for e in ev if e["code"] == "S")
    print(f"{t}: Form4 {len(f4)}份(买事件{nb} 卖事件{ns}) 8-K {len(k8)}份", flush=True)

with open("info_insider_hist.json", "w") as f:
    json.dump(insider, f, ensure_ascii=False, indent=1)
with open("info_8k_hist.json", "w") as f:
    json.dump(eightk, f, ensure_ascii=False, indent=1)
print("已写入 info_insider_hist.json / info_8k_hist.json")
