"""8-K 附件修复 v3：不用文件名猜附件，改用 EDGAR 官方申报索引页
（{acc}-index.htm 的 Document Format Files 表，含每份文档的 Type 列）定位 EX-99，
只补此前 147 份未找到的事件，合并写回 info_8k_exhibits.json。
诊断结论：漏检主因是 NVDA 式命名（q4fy23pr.htm 无 "99" 字样）；另一类是本来
就没有 EX-99 的申报（多为 7.01，正文即全部内容），修不好也不硬凑。
"""
import json, re, sys, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
LO, HI = "2023-01-01", "2026-10-09"

def strip_html(txt):
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return re.sub(r"\s+", " ", txt)

_FILL = r"(?:\s+(?:its|the|full[-\s]year|annual|fiscal|fy\s?\d{2,4}|quarterly|revenue|sales|eps|earnings|operating|adjusted|adj\.|non-gaap|for\s+20\d\d|20\d\d))*"
_NOUN = r"\s+(guidance|outlook)\b"
PATTERNS = {
    "withdraw": r"(withdraw|withdrew|suspend|withhold)s?" + _FILL + _NOUN,
    "down": r"(lower|lowers|lowered|reduce|reduces|reduced|cut|cuts|trim|trims|slash|slashes|decrease|decreases|decreased)s?" + _FILL + _NOUN,
    "up": r"(raise|raises|raised|increase|increases|increased|boost|boosts|lift|lifts|hike|hikes)s?" + _FILL + _NOUN,
    "maintain": r"(reaffirm|reaffirms|reaffirmed|maintain|maintains|maintained|reiterat\w+)" + _FILL + _NOUN,
    "initiate": r"(initiate|initiates|initiated|introduc\w+|provid\w+|issues?|issuing)" + _FILL + _NOUN,
}
PRIORITY = ["withdraw", "down", "up", "maintain", "initiate"]
BUYBACK_RE = re.compile(r"(share repurchase|buyback|repurchase program|repurchase authorization)", re.I)
DIV_RE = re.compile(r"(declares?\s+(a\s+)?quarterly\s+dividend|dividend\s+of\s+\$|increases?\s+(its\s+)?dividend)", re.I)

def guidance(text):
    hits = {}
    low = text.lower()
    for k, pat in PATTERNS.items():
        ms = []
        for m in re.finditer(pat, low):
            s = max(0, m.start() - 40)
            ms.append(text[s:m.end() + 40].replace("\n", " ").strip()[:170])
            if len(ms) >= 2:
                break
        if ms:
            hits[k] = ms
    direction = next((k for k in PRIORITY if k in hits), None)
    return direction, hits

def all_8k(t):
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
            if rec["form"][i] in ("8-K", "8-K/A") and LO <= rec["filingDate"][i] < HI:
                rows.append({"filed": rec["filingDate"][i], "acc": rec["accessionNumber"][i].replace("-", "")})
    return int(cik), rows

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
HREF_RE = re.compile(r'href="[^"]*/([^"/]+)"')

def find_ex99(cik, acc):
    """返回 (doc_name or None, 失败原因 or None)。优先 EX-99.1，其次 EX-99。"""
    dashed = f"{acc[:10]}-{acc[10:12]}-{acc[12:]}"
    try:
        html = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{dashed}-index.htm", ua=SEC_UA)
    except Exception as e:
        return None, f"索引页失败:{type(e).__name__}"
    cands = []
    for row in ROW_RE.findall(html):
        m = re.search(r">\s*(EX-99(?:\.\d+)?)\s*<", row, re.I)
        if not m:
            continue
        href = HREF_RE.search(row)
        if href:
            cands.append((m.group(1).upper(), href.group(1)))
    if not cands:
        return None, "索引中无EX-99（该申报无此附件）"
    cands.sort(key=lambda c: (c[0] != "EX-99.1", len(c[1])))
    return cands[0][1], None

ex = json.load(open("info_8k_exhibits.json"))
acc_map = {}
for t in TICKS:
    cik, rows = all_8k(t)
    for r in rows:
        acc_map[(t, r["filed"])] = (cik, r["acc"])
print("accession 映射完成", flush=True)

jobs = []
for t, evs in ex.items():
    for i, e in enumerate(evs):
        if not e["ex99_found"] and (t, e["filed"]) in acc_map:
            cik, acc = acc_map[(t, e["filed"])]
            jobs.append((t, i, cik, acc))
print(f"待修复 {len(jobs)} 份", flush=True)

def process(job):
    t, i, cik, acc = job
    doc, err = find_ex99(cik, acc)
    if doc is None:
        return t, i, None, err
    try:
        raw = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}", ua=SEC_UA)
    except Exception as e:
        return t, i, None, f"附件拉取失败:{type(e).__name__}"
    time.sleep(0.04)
    text = strip_html(raw) if "<" in raw[:3000] else raw
    g, hits = guidance(text)
    return t, i, {"ex99_found": True, "ex_file": doc, "guidance": g, "hits": hits,
                  "buyback": bool(BUYBACK_RE.search(text)), "dividend": bool(DIV_RE.search(text)),
                  "text_len": len(text), "fix_v3": True}, None

reasons = Counter()
recovered = 0
with ThreadPoolExecutor(max_workers=6) as pool:
    for t, i, upd, err in pool.map(process, jobs):
        if upd:
            ex[t][i].update(upd)
            ex[t][i].pop("error", None)
            recovered += 1
        else:
            ex[t][i]["fix_v3_note"] = err
            reasons[err] += 1

with open("info_8k_exhibits.json", "w") as f:
    json.dump(ex, f, ensure_ascii=False, indent=1)

total = sum(len(v) for v in ex.values())
found = sum(1 for evs in ex.values() for e in evs if e["ex99_found"])
c = Counter(e["guidance"] for evs in ex.values() for e in evs if e["guidance"])
print(f"修复回收 {recovered} 份；总覆盖 {found}/{total}；指引分布 {dict(c)}")
print("仍未找到原因:", dict(reasons))
