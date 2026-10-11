"""8-K 附件解析：把事件层从"事项代码"做实到"内容信号"。
对含 2.02（业绩）/7.01（信息披露）的 8-K（2023-01 ~ 2026-10），拉 EDGAR 附件
EX-99.1 业绩稿，用规则提取指引方向（上调/下调/维持/撤回/首次给出）及回购/分红标记，
每条保存命中短语作证据。输出 info_8k_exhibits.json。
局限：规则法只认常见英文表述，数值型指引（给出区间但未用方向词）会漏检；
同一稿多方向冲突时按 撤回>下调>上调>维持 取最重者，并保存全部命中。
"""
import json, re, sys, time
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
                rows.append({"filed": rec["filingDate"][i],
                             "acc": rec["accessionNumber"][i].replace("-", ""),
                             "accepted": (rec.get("acceptanceDateTime") or [""] * n)[i]})
    return int(cik), rows

# 已分类的事项代码（前序脚本产物），按 (ticker, filed) 对齐
old_items = {}
for fn in ("info_8k_hist.json", "info_8k_2026.json"):
    d = json.load(open(fn))
    for t, evs in d.items():
        for e in evs:
            old_items[(t, e["filed"])] = e.get("key_items") or e.get("items") or []

# 方向词必须直接作用于 guidance/outlook 名词（中间只允许限定词），
# 避免 "lower than forecasted income" 这类业绩比较句式误报（v1 教训）。
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

def process(job):
    t, cik, r, items = job
    try:
        idx = json.loads(fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc']}/index.json", ua=SEC_UA))
        names = [it["name"] for it in idx["directory"]["item"]]
        # v2：附件命名五花八门（ex-991/exhibit991/ex99_1/pr…），凡含 99 的 htm/txt 都算候选
        cands = [n for n in names if "99" in n and n.lower().endswith((".htm", ".html", ".txt"))
                 and "xbrl" not in n.lower() and not n.lower().endswith(".xml")]
        cands.sort(key=lambda n: (0 if re.search(r"99[-_.]?1", n) else 1,
                                  0 if n.lower().startswith(("ex", "exhibit")) else 1, len(n)))
        if not cands:
            return {"filed": r["filed"], "accepted": r["accepted"], "items": items,
                    "ex99_found": False, "guidance": None, "hits": {}, "buyback": False, "dividend": False}
        raw = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{r['acc']}/{cands[0]}", ua=SEC_UA)
        time.sleep(0.04)
        text = strip_html(raw) if "<" in raw[:3000] else raw
        g, hits = guidance(text)
        return {"filed": r["filed"], "accepted": r["accepted"], "items": items,
                "ex99_found": True, "ex_file": cands[0], "guidance": g, "hits": hits,
                "buyback": bool(BUYBACK_RE.search(text)), "dividend": bool(DIV_RE.search(text)),
                "text_len": len(text)}
    except Exception as e:
        return {"filed": r["filed"], "accepted": r["accepted"], "items": items,
                "ex99_found": False, "guidance": None, "hits": {}, "buyback": False,
                "dividend": False, "error": type(e).__name__}

jobs, per_ticker_counts = [], {}
for t in TICKS:
    cik, rows = all_8k(t)
    kept = []
    for r in rows:
        items = old_items.get((t, r["filed"]), [])
        if "2.02" in items or "7.01" in items:
            kept.append(r)
            jobs.append((t, cik, r, items))
    per_ticker_counts[t] = (len(rows), len(kept))
    print(f"{t}: 8-K {len(rows)}份，含业绩/披露 {len(kept)}份", flush=True)

results = {t: [] for t in TICKS}
with ThreadPoolExecutor(max_workers=6) as ex:
    for (t, cik, r, items), out in zip(jobs, ex.map(process, jobs)):
        results[t].append(out)
for t in results:
    results[t].sort(key=lambda e: e["accepted"] or e["filed"])

with open("info_8k_exhibits.json", "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=1)

from collections import Counter
c = Counter()
found = 0
for t, evs in results.items():
    for e in evs:
        found += bool(e["ex99_found"])
        if e["guidance"]:
            c[e["guidance"]] += 1
print(f"共 {len(jobs)} 份，找到 EX-99 附件 {found} 份；指引方向分布 {dict(c)}")
print("已写入 info_8k_exhibits.json")
