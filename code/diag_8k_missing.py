"""诊断：147 份未找到 EX-99 的 8-K 到底卡在哪。用官方申报索引页（-index.htm）
的文档类型列核对：是没有附件、附件类型不是 EX-99、还是文件名没被规则命中。"""
import json, re, sys
from collections import Counter
sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]
LO, HI = "2023-01-01", "2026-10-09"

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

ex = json.load(open("info_8k_exhibits.json"))
missing = {(t, e["filed"]) for t, evs in ex.items() for e in evs if not e["ex99_found"]}
print("未找到附件总数:", len(missing))

# 建 accession 映射
acc_map = {}
for t in TICKS:
    cik, rows = all_8k(t)
    for r in rows:
        acc_map[(t, r["filed"])] = (cik, r["acc"])

# 索引页文档行：文件名 + 类型
ROW_RE = re.compile(r'<tr[^>]*>\s*<td[^>]*>.*?</td>\s*<td[^>]*><a href="([^"]+)">[^<]+</a></td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>([^<]*)</td>', re.S)

causes = Counter()
examples = {}
sample = sorted(missing)[:400]
for (t, filed) in sample:
    cik, acc = acc_map.get((t, filed), (None, None))
    if acc is None:
        causes["无accession映射"] += 1
        continue
    try:
        html = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{acc[:10]}-{acc[10:12]}-{acc[12:]}-index.htm", ua=SEC_UA)
    except Exception:
        try:
            html = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{acc}-index.htm", ua=SEC_UA)
        except Exception as e2:
            causes[f"索引页拉取失败:{type(e2).__name__}"] += 1
            continue
    rows = ROW_RE.findall(html)
    types = [(doc.split("/")[-1], typ.strip()) for doc, _desc, typ in rows]
    ex99 = [n for n, ty in types if ty.upper().startswith("EX-99")]
    if ex99:
        hit = any("99" in n and n.lower().endswith((".htm", ".html", ".txt")) for n, ty in types)
        c = "有EX99但文件名规则漏检" if not hit else "有EX99且规则应命中(异常)"
        causes[c] += 1
        examples.setdefault(c, []).append((t, filed, ex99, [x for x in types][:5]))
    else:
        ex_types = sorted({ty for _, ty in types if ty.upper().startswith("EX-")})
        c = f"无EX99附件(类型:{','.join(ex_types) if ex_types else '仅正文'})"
        causes[c] += 1
        examples.setdefault(c, []).append((t, filed, [], [x for x in types][:5]))

print("\n原因分布（样本 %d）:" % len(sample))
for c, n in causes.most_common():
    print(f"  {n:4d}  {c}")
for c, exs in examples.items():
    print(f"\n--- {c} 样例 ---")
    for t, f, ex99, types in exs[:3]:
        print(" ", t, f, "EX99:", ex99, "文档:", types)
