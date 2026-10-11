"""免费信息面接入之二：SEC 8-K 重大事件流（2026-01-01 起，32 只票）。
按 Item 编号粗分类（业绩/指引/并购/高管变动等），时点用申报时间 acceptanceDateTime，
天然 point-in-time。输出 info_8k_2026.json。
"""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
SINCE = "2026-01-01"

ITEM_MEANING = {
    "1.01": "重大协议签订", "1.02": "重大协议终止", "1.03": "破产/接管",
    "2.01": "收购或资产处置完成", "2.02": "业绩发布（季报成绩单）", "2.03": "新增重大债务",
    "2.04": "表外债务触发", "2.05": "退出/处置成本（重组）", "2.06": "资产减值",
    "3.01": "退市/上市通知", "3.02": "未注册证券销售", "3.03": "证券条款修改",
    "4.01": "审计师变更", "4.02": "财报不可信赖（重述）",
    "5.01": "控制权变更", "5.02": "高管/董事变动", "5.03": "章程修订",
    "5.04": "福利计划冻结", "5.05": "道德准则修订", "5.06": "壳公司变更",
    "5.07": "股东大会投票结果", "5.08": "股东提名事项",
    "6.01": "ABS尽职调查", "6.02": "担保人变更", "6.03": "从属关系变更",
    "6.04": "担保失败", "6.05": "证券登记更新",
    "7.01": "信息披露（常含指引/业绩材料）", "8.01": "其他重大事件", "9.01": "财务报表与附件",
}
KEY_ITEMS = {"1.01", "1.02", "2.01", "2.02", "2.05", "2.06", "4.02", "5.01", "5.02", "7.01", "8.01"}

ITEM_RE = re.compile(r"Item\s+(\d\.\d{2})", re.I)

def strip_html(txt):
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt)

def fetch_items(job):
    cik_int, acc, doc, filed, acc_dt = job
    try:
        raw = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}", ua=SEC_UA)
        time.sleep(0.05)
        text = strip_html(raw) if "<" in raw[:2000] else raw
        items = sorted({m.group(1) for m in ITEM_RE.finditer(text)})
        return {"filed": filed, "accepted": acc_dt, "items": items,
                "key_items": [i for i in items if i in KEY_ITEMS],
                "summary": {i: ITEM_MEANING.get(i, "") for i in items},
                "doc": doc}
    except Exception:
        return {"filed": filed, "accepted": acc_dt, "items": [], "key_items": [], "summary": {}, "doc": doc}

out = {}
for t in TICKS:
    cik = cik_of(t)
    sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", ua=SEC_UA))
    rec = sub["filings"]["recent"]
    idx = [i for i in range(len(rec["form"]))
           if rec["form"][i] in ("8-K", "8-K/A") and rec["filingDate"][i] >= SINCE]
    jobs = [(int(cik), rec["accessionNumber"][i].replace("-", ""),
             rec["primaryDocument"][i], rec["filingDate"][i],
             rec.get("acceptanceDateTime", [""] * len(rec["form"]))[i]) for i in idx]
    with ThreadPoolExecutor(max_workers=6) as ex:
        evs = list(ex.map(fetch_items, jobs))
    evs.sort(key=lambda e: e["accepted"] or e["filed"])
    out[t] = evs
    from collections import Counter
    c = Counter(i for e in evs for i in e["key_items"])
    print(f"{t}: 8-K {len(evs)}份 关键事项 {dict(c)}", flush=True)

with open("info_8k_2026.json", "w") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("已写入 info_8k_2026.json")
