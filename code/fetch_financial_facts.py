"""SEC companyfacts -> financial_facts（PIT 财报库地基）。
一行 = 一条财务数据的一个申报版本：同一报告期若被修订/重述，多个版本按
filed 日期并存。之后给任意历史日期 D 打分，只取 filed<=D 的最新版，杜绝
前视。源：data.sec.gov companyfacts（免费官方），只保留 10-K/10-Q（含 /A）。
"""
import json
import sys
import time

sys.path.insert(0, ".")
from factor_scorecard_v2 import fetch, cik_of, SEC_UA
import db
from paper_trade_2026 import TICKERS

TICKS = TICKERS + ["MU", "GOOGL"]

CONCEPTS = {
    "revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt_cur": ["DebtCurrent", "LongTermDebtCurrent"],
    "debt_noncur": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "liabilities": ["Liabilities"],
    "assets": ["Assets"],
    "assets_cur": ["AssetsCurrent"],
    "liab_cur": ["LiabilitiesCurrent"],
    "shares_out": ["CommonStockSharesOutstanding"],
}
DEI_CONCEPTS = {"shares_out_dei": ["EntityCommonStockSharesOutstanding"]}
FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")

conn = db.get_conn()
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS financial_facts (
    ticker text NOT NULL, concept text NOT NULL, tag text NOT NULL,
    unit text NOT NULL, period_start date, period_end date NOT NULL,
    form text, filed date NOT NULL, accession text,
    val numeric,
    PRIMARY KEY (ticker, concept, tag, unit, period_start, period_end,
                 form, filed, accession)
)""")
conn.commit()

total = 0
for t in TICKS:
    raw = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_of(t)}.json",
                ua=SEC_UA)
    facts = json.loads(raw)["facts"]
    rows = []
    for concept, tags in CONCEPTS.items():
        for tag in tags:
            node = facts.get("us-gaap", {}).get(tag)
            if not node:
                continue
            for unit, arr in node.get("units", {}).items():
                if unit not in ("USD", "USD/shares", "shares"):
                    continue
                for e in arr:
                    if e.get("form") not in FORMS or "filed" not in e:
                        continue
                    rows.append((t, concept, tag, unit,
                                 e.get("start") or e["end"],
                                 e["end"], e.get("form"), e["filed"],
                                 e.get("accn"), e["val"]))
    for concept, tags in DEI_CONCEPTS.items():
        for tag in tags:
            node = facts.get("dei", {}).get(tag)
            if not node:
                continue
            for unit, arr in node.get("units", {}).items():
                for e in arr:
                    if "filed" not in e:
                        continue
                    rows.append((t, concept, tag, unit, e["end"], e["end"],
                                 e.get("form"), e["filed"], e.get("accn"),
                                 e["val"]))
    cur.executemany(
        "INSERT INTO financial_facts VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING", rows)
    conn.commit()
    total += len(rows)
    print(f"{t}: {len(rows)} 条事实")
    time.sleep(0.15)
print(f"financial_facts 合计 {total} 条")
conn.close()
