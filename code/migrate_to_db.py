"""把 quant-practice 目录下的历史数据文件一次性迁入 PostgreSQL quant 库。
之后新数据由各抓取脚本直接写库（见 fetch_estimates_daily.py）。
运行：.venv/bin/python migrate_to_db.py
"""
import glob
import json
import pathlib

import pandas as pd

import db

ROOT = pathlib.Path(__file__).resolve().parent
conn = db.get_conn()
conn.autocommit = False
cur = conn.cursor()
cur.execute((ROOT / "schema.sql").read_text())
conn.commit()
print("表结构就绪")


def upsert_many(sql, rows, label):
    if not rows:
        print(label, "0 行，跳过")
        return
    cur.executemany(sql, rows)
    conn.commit()
    print(f"{label}: {len(rows)} 行")


# ---------- 预期快照（2026-10-08 第一份） ----------
snap = json.loads((ROOT / "estimates_snapshot_2026-10-08.json").read_text())
SD = "2026-10-08"
rows_s, rows_t, rows_r, rows_p, rows_e, rows_g = [], [], [], [], [], []
for t, o in snap.items():
    rows_s.append((SD, t))
    for per, v in (o.get("eps_trend") or {}).items():
        rows_t.append((SD, t, per, v.get("cur"), v.get("ago90"),
                       v.get("rev90_pct")))
    for per, v in (o.get("eps_revisions") or {}).items():
        rows_r.append((SD, t, per, v.get("up30"), v.get("down30"),
                       v.get("up7"), v.get("down7")))
    pt = o.get("price_target") or {}
    if pt.get("mean") is not None:
        rows_p.append((SD, t, pt.get("current"), pt.get("mean"),
                       pt.get("median"), pt.get("high"), pt.get("low")))
    for h in o.get("earnings_hist") or []:
        rows_e.append((t, h["quarter"], h.get("eps_actual"), h.get("eps_est"),
                       h.get("surprise_pct"), SD))
    for a in o.get("grade_actions") or []:
        rows_g.append((t, a["date"], a.get("firm", ""), a.get("from"),
                       a.get("to"), a.get("action", "")))
upsert_many("INSERT INTO estimates_snapshot VALUES (%s,%s,'yfinance') "
            "ON CONFLICT DO NOTHING", rows_s, "estimates_snapshot")
upsert_many("INSERT INTO estimates_trend VALUES (%s,%s,%s,%s,NULL,NULL,NULL,%s,%s) "
            "ON CONFLICT DO NOTHING", rows_t, "estimates_trend")
upsert_many("INSERT INTO estimates_revisions VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", rows_r, "estimates_revisions")
upsert_many("INSERT INTO price_targets VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", rows_p, "price_targets")
upsert_many("INSERT INTO earnings_history VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", rows_e, "earnings_history")
upsert_many("INSERT INTO grade_actions VALUES (%s,%s,%s,%s,%s,%s,'yahoo') "
            "ON CONFLICT DO NOTHING", rows_g, "grade_actions")

# ---------- 行情与因子 CSV ----------
def load_csv(path, sql, label, cols_order=None):
    df = pd.read_csv(path)
    if cols_order:
        df = df[cols_order]
    rows = [tuple(None if pd.isna(v) else v for v in r)
            for r in df.itertuples(index=False, name=None)]
    upsert_many(sql, rows, label)

load_csv(ROOT / "options_vix_history.csv",
         "INSERT INTO vix_daily VALUES (%s,%s) ON CONFLICT DO NOTHING",
         "vix_daily")
load_csv(ROOT / "options_etf_history_daily.csv",
         "INSERT INTO etf_options_daily VALUES (%s,%s,%s,%s,%s,%s,%s) "
         "ON CONFLICT DO NOTHING", "etf_options_daily",
         cols_order=["symbol", "date", "pc_vol", "pc_oi", "total_vol",
                     "iv_atm", "skew25"])
load_csv(ROOT / "options_snapshot_daily.csv",
         "INSERT INTO options_snapshot_daily (ticker,date,price,iv30,"
         "iv30_chg_pct,pc_vol,pc_oi,call_vol,put_vol,skew25,asof) "
         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
         "options_snapshot_daily",
         cols_order=["ticker", "date", "price", "iv30", "iv30_chg_pct",
                     "pc_vol", "pc_oi", "call_vol", "put_vol", "skew25",
                     "asof"])

vf = pd.read_csv(ROOT / "volume_factors_daily.csv")
vf["date"] = pd.to_datetime(vf["date"]).dt.date
rows = [(r.date, r.ticker, r.vol_ratio, r.up_share, r.accdist,
         None if pd.isna(r.dist_days) else int(r.dist_days), r.obv_trend,
         bool(r.price_obv_div), bool(r.breakout), bool(r.climax), r.vol_score)
        for r in vf.itertuples()]
upsert_many("INSERT INTO volume_factors_daily VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            [tuple(None if (isinstance(v, float) and pd.isna(v)) else v
                   for v in r) for r in rows], "volume_factors_daily")

# HF 个股 IV 面板（3.16M 行，COPY 灌入；已灌过则跳过）
cur.execute("SELECT count(*) FROM stock_iv_panel")
if cur.fetchone()[0] >= 3_000_000:
    print("stock_iv_panel 已存在，跳过")
else:
    cur.execute("TRUNCATE stock_iv_panel")
    conn.commit()
    ivdf = pd.read_csv(ROOT / "hf_stock_iv" / "data_IV_USA.csv")
    ivdf = ivdf.drop(columns=[c for c in ivdf.columns
                              if c.startswith("Unnamed")])
    with cur.copy("COPY stock_iv_panel FROM STDIN") as cp:
        for r in ivdf.itertuples(index=False, name=None):
            cp.write_row(tuple(None if (isinstance(v, float) and pd.isna(v))
                               else v for v in r))
    conn.commit()
    print(f"stock_iv_panel: {len(ivdf)} 行")

# ETF 标的日线（parquet）
for sym in ("spy", "qqq"):
    pxdf = pd.read_parquet(ROOT / "etf_options" / sym / "underlying_prices.parquet")
    rows = [(sym.upper(), pd.to_datetime(r.date).date(), r.open, r.high,
             r.low, r.close, r.adjusted_close, int(r.volume))
            for r in pxdf.itertuples()]
    cur.executemany("INSERT INTO prices_daily VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                    "'github-etf-dataset') ON CONFLICT DO NOTHING", rows)
    conn.commit()
    print(f"prices_daily[{sym.upper()}]: {len(rows)} 行")

# ---------- 信息面 JSON ----------
ins_rows = []
for f in ("info_insider_hist.json", "info_insider_events_2026.json"):
    d = json.loads((ROOT / f).read_text())
    for t, evs in d.items():
        for e in evs:
            ins_rows.append((t, e.get("filed"), e.get("date"), e.get("code"),
                             e.get("shares"), e.get("price"), e.get("owner"),
                             bool(e.get("officer")), bool(e.get("director"))))
upsert_many("INSERT INTO insider_transactions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", ins_rows, "insider_transactions")

summ = json.loads((ROOT / "info_insider_2026.json").read_text())
upsert_many("INSERT INTO insider_summary VALUES (%s,%s) "
            "ON CONFLICT (ticker) DO UPDATE SET payload=EXCLUDED.payload",
            [(t, json.dumps(v)) for t, v in summ.items()], "insider_summary")

fk_rows = []
for f in ("info_8k_hist.json", "info_8k_2026.json"):
    d = json.loads((ROOT / f).read_text())
    for t, evs in d.items():
        for e in evs:
            fk_rows.append((t, e.get("filed"), e.get("accepted"),
                            e.get("items"), e.get("key_items"),
                            json.dumps(e.get("summary") or {}),
                            e.get("doc") or "", None, None, None, None,
                            None, None, None))
upsert_many("INSERT INTO eightk_filings VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", fk_rows, "eightk_filings")

ex = json.loads((ROOT / "info_8k_exhibits.json").read_text())
ex_rows = []
for t, evs in ex.items():
    for e in evs:
        ex_rows.append((t, e.get("filed"), e.get("accepted"), e.get("items"),
                        e.get("ex_file") or "", e.get("ex99_found"),
                        json.dumps(e["guidance"]) if e.get("guidance") else None,
                        json.dumps(e.get("hits") or {}), e.get("buyback"),
                        e.get("dividend"), e.get("text_len")))
cur.executemany(
    "INSERT INTO eightk_filings (ticker,filed,accepted,items,doc,ex99_found,"
    "guidance,hits,buyback,dividend,text_len) VALUES "
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", ex_rows)
conn.commit()
print(f"eightk_exhibits 并入: {len(ex_rows)} 行")

si = json.loads((ROOT / "info_short_interest.json").read_text())
si_rows = []
for t, evs in si.items():
    for e in evs:
        m, dd, y = e["date"].split("/")
        si_rows.append((t, f"{y}-{m}-{dd}", int(e["short_shares"]),
                        int(e["avg_daily_vol"]), e["days_to_cover"]))
upsert_many("INSERT INTO short_interest VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING", si_rows, "short_interest")

# ---------- 纸面模拟记录 ----------
CN = {"日期": "trade_date", "代码": "ticker", "动作": "action", "股数": "shares",
      "价格": "price", "金额": "amount", "触发": "trigger", "层": "layer"}
for f in sorted(glob.glob(str(ROOT / "paper_trade_*_trades.csv"))):
    ver = pathlib.Path(f).stem.replace("_trades", "")
    d = pd.read_csv(f).rename(columns=CN)
    if d.empty:
        continue
    d["trade_date"] = pd.to_datetime(d["trade_date"]).dt.date
    rows = [(ver, r.trade_date, r.ticker, r.action, r.shares, r.price,
             r.amount, getattr(r, "trigger", None), getattr(r, "layer", None))
            for r in d.itertuples()]
    upsert_many("INSERT INTO paper_trades VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING", rows, f"paper_trades[{ver}]")
for f in sorted(glob.glob(str(ROOT / "paper_trade_*_equity.csv"))):
    ver = pathlib.Path(f).stem.replace("_equity", "")
    d = pd.read_csv(f)
    if d.empty:
        continue
    rows = [(ver, pd.to_datetime(r.date).date(), r.equity)
            for r in d.itertuples()]
    upsert_many("INSERT INTO paper_equity VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING", rows, f"paper_equity[{ver}]")

# ---------- 其余 JSON 落 raw_json ----------
for f in ("factor_framework_v3_results.json",
          "screen_asof_2026_01_01_results.json"):
    d = json.loads((ROOT / f).read_text())
    cur.execute("INSERT INTO raw_json VALUES (%s,'',%s) ON CONFLICT DO NOTHING",
                (f, json.dumps(d)))
    conn.commit()
    print(f"raw_json: {f}")

cur.execute("""
SELECT table_name FROM information_schema.tables
WHERE table_schema='public' ORDER BY 1""")
print("\n=== 入库汇总 ===")
for (tn,) in cur.fetchall():
    cur.execute(f'SELECT count(*) FROM "{tn}"')
    print(f"{tn}: {cur.fetchone()[0]} 行")
conn.close()
print("迁移完成")
