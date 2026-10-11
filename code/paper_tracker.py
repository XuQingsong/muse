"""
前向纸面组合跟踪器（用户 2026-10-10 拍板口径）。

规则摘要：
- 初始组合：2026-10-09 纸面计划的 10 只，成本按计划参考价记账，现金为模型账户余款。
- 新买入：每个美股交易日盘前用 v18 同口径重扫 30 只；未持有且过线的标的生成限价触发单，
  指定价 = 信号日收盘价。盘中价格触及才买入，不追高。
- 止损/止盈：按层预设价线。止损清仓；止盈卖出当时持仓的 1/3（一次性）。
  防守 止损-8%/止盈+15%；普通 止损-12%/止盈+20%；进攻 止损-18%/止盈+25%。
- 同一日/同一分钟同时碰到止损与止盈：优先使用 1 分钟线按时间先后判定；
  若同一分钟内双碰，则按该分钟 OHLC 路径作近似推断并在报告中标注。
- 基本面逻辑死亡（v18 DEATH_REDLINES）只在信息可见后生成下一交易日开盘退出单，不倒填成交。
- 纸面账本，不接 IBKR、不动真实资金。未来交易按每边 0.1% 成本记账；初始 10 只不倒扣买入成本。

CLI：
  .venv/bin/python paper_tracker.py init
  .venv/bin/python paper_tracker.py premarket [--date YYYY-MM-DD]
  .venv/bin/python paper_tracker.py settle --date YYYY-MM-DD
"""
import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_trade_v18_full30 as V
from today_plan import latest_signal, buy_limit
from factor_scorecard_v2 import get_history

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_DIR = os.path.join(BASE_DIR, "paper_live")
DEFAULT_STATE = os.path.join(LIVE_DIR, "state.json")
REPORT_DIR = os.path.join(LIVE_DIR, "reports")
TICKERS30 = None  # 从 today_plan 导入，避免重复维护
from today_plan import TICKERS30

COST = 0.001
LINES = {
    "防守": {"stop_pct": -0.08, "take_pct": 0.15},
    "普通": {"stop_pct": -0.12, "take_pct": 0.20},
    "进攻": {"stop_pct": -0.18, "take_pct": 0.25},
}
INITIAL_PLAN = [
    # ticker, tier, 目标金额（美元）, 成本参考价（2026-10-08 收盘）
    ("CRM", "进攻", 21120, 227.80),
    ("MU", "进攻", 40000, 1035.84),
    ("AVGO", "进攻", 40000, 360.14),
    ("PLTR", "进攻", 40000, 198.78),
    ("MRVL", "进攻", 8800, 274.66),
    ("GOOGL", "普通", 72000, 348.29),
    ("META", "普通", 72000, 720.89),
    ("NVDA", "普通", 57600, 230.48),
    ("UNH", "防守", 50400, 370.95),
    ("CAT", "普通", 38400, 796.18),
]


def _round2(x):
    return round(float(x), 2)


def make_lines(tier, entry_price):
    cfg = LINES[tier]
    return {
        "stop_price": _round2(entry_price * (1 + cfg["stop_pct"])),
        "take_price": _round2(entry_price * (1 + cfg["take_pct"])),
        "stop_pct": cfg["stop_pct"],
        "take_pct": cfg["take_pct"],
    }


def initial_state():
    positions = {}
    cash = 1_000_000.0
    for ticker, tier, budget, entry in INITIAL_PLAN:
        shares = int(budget // entry)
        invested = shares * entry
        cash -= invested
        lines = make_lines(tier, entry)
        positions[ticker] = {
            "ticker": ticker,
            "tier": tier,
            "shares": shares,
            "entry_price": entry,
            "entry_date": "2026-10-09",
            "entry_note": "纸面计划参考价记账（信号日 2026-10-08 收盘）",
            "last_price": entry,
            **lines,
            "take_profit_done": False,
        }
    return {
        "version": 1,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "currency": "USD",
        "initial_equity": 1_000_000.0,
        "cash": _round2(cash),
        "positions": positions,
        "pending_buys": [],
        "pending_liquidations": [],
        "history": [],
        "settled_sessions": [],
        "black_swan_pause_until": None,
        "rules": {
            "cost_per_side": COST,
            "lines": LINES,
            "same_day_both_hit": "按1分钟线时间先后；同一分钟双碰按OHLC路径近似并标注",
            "buy_trigger": "信号日收盘价限价触发；开盘已低于触发价按开盘价",
            "universe": TICKERS30,
        },
    }


def load_state(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def write_report(name, payload):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    return path


def _signal_map():
    out = {}
    failures = {}
    for ticker in TICKERS30:
        try:
            _, row = latest_signal(ticker)
            out[ticker] = row
        except Exception as exc:  # 单票数据失败不拖垮整轮
            failures[ticker] = f"{type(exc).__name__}: {str(exc)[:180]}"
    return out, failures


def _spy_snapshot():
    df = get_history("SPY")
    closes = df["close"]
    return {
        "date": str(df.index[-1].date()),
        "close": float(closes.iloc[-1]),
        "ret1d_pct": float((closes.iloc[-1] / closes.iloc[-2] - 1) * 100),
        "ret5d_pct": float((closes.iloc[-1] / closes.iloc[-6] - 1) * 100),
        "vs_ma200_pct": float((closes.iloc[-1] / closes.rolling(200).mean().iloc[-1] - 1) * 100),
    }


def _add_weekdays(d, n):
    step = 1 if n >= 0 else -1
    remaining = abs(n)
    while remaining:
        d += timedelta(days=step)
        if d.weekday() < 5:
            remaining -= 1
    return d


def premarket(state_path, session_date=None):
    state = load_state(state_path)
    session = session_date or date.today().isoformat()
    # 幂等：同一交易日已生成盘前单就不重复生成。
    if any(o.get("session_date") == session for o in state.get("pending_buys", [])):
        report = {"type": "premarket", "session_date": session, "status": "already_generated"}
        path = write_report(f"premarket_{session}.json", report)
        return report, path

    signals, failures = _signal_map()
    spy = _spy_snapshot()
    signal_date = spy["date"]

    # 给持仓更新最新价，并检查基本面死亡（只生成下一交易日开盘退出单）。
    for ticker, pos in state["positions"].items():
        sig = signals.get(ticker)
        if sig:
            pos["last_price"] = float(sig["price"])
            pos["last_signal_date"] = sig["date"]
    state["pending_liquidations"] = [
        x for x in state.get("pending_liquidations", []) if x.get("session_date") != session
    ]
    liquidation_notes = []
    for ticker, pos in list(state["positions"].items()):
        sig = signals.get(ticker)
        if not sig:
            continue
        death_hits = [x for x in sig.get("redlines", []) if x in V.DEATH_REDLINES]
        if sig.get("excluded") and death_hits:
            state["pending_liquidations"].append({
                "session_date": session,
                "ticker": ticker,
                "reason": "基本面逻辑死亡：" + "；".join(death_hits),
                "signal_date": sig["date"],
            })
            liquidation_notes.append({"ticker": ticker, "reason": death_hits, "signal_date": sig["date"]})

    # 黑天鹅：以信号日 SPY 单日/5日跌幅判定，暂停新买 3 个交易日。
    pause_until = state.get("black_swan_pause_until")
    black_swan = spy["ret1d_pct"] <= -4 or spy["ret5d_pct"] <= -8
    if black_swan:
        pause_until = _add_weekdays(date.fromisoformat(signal_date), 3).isoformat()
        state["black_swan_pause_until"] = pause_until
    paused = bool(pause_until and session <= pause_until)

    equity = state["cash"] + sum(
        p["shares"] * float(p.get("last_price", p["entry_price"])) for p in state["positions"].values()
    )
    tier_mv = {k: 0.0 for k in V.TIER_BUDGET}
    tier_counts = {k: 0 for k in V.TIER_BUDGET}
    for p in state["positions"].values():
        tier_mv[p["tier"]] += p["shares"] * float(p.get("last_price", p["entry_price"]))
        tier_counts[p["tier"]] += 1

    candidates = []
    for ticker, sig in signals.items():
        if ticker in state["positions"]:
            continue
        if not (sig.get("entry_pass") and not sig.get("overheat") and sig.get("fund_score", 0) >= 45 and sig.get("tier")):
            continue
        if sig["tier"] == "防守" and sig.get("fund_score", 0) < 55:
            continue
        candidates.append((sig.get("rank") or 0, ticker, sig))
    candidates.sort(key=lambda x: x[0], reverse=True)
    ordered = [x for x in candidates if x[2]["tier"] == "进攻"] + [x for x in candidates if x[2]["tier"] != "进攻"]

    orders = []
    skipped = []
    projected_counts = dict(tier_counts)
    projected_mv = dict(tier_mv)
    available_cash = state["cash"]
    if paused:
        skipped.append({"reason": f"黑天鹅暂停新买至 {pause_until}", "spy": spy})
    else:
        for _, ticker, sig in ordered:
            tier = sig["tier"]
            if projected_counts[tier] >= V.TIER_MAXNAMES[tier] or sum(projected_counts.values()) >= 12:
                skipped.append({"ticker": ticker, "reason": "名额已满"})
                continue
            frac = V.opening_fraction(tier, sig.get("certainty", "中"))
            if sig.get("insider_boost", 1.0) > 1.0:
                frac = min(0.80, frac * sig["insider_boost"])
            value = frac * V.TIER_CAP[tier] * equity
            if sig.get("dev50", 0) > 15:
                value *= 0.5
            limit = buy_limit(tier, projected_mv, equity, available_cash)
            if projected_mv[tier] + value > limit:
                skipped.append({"ticker": ticker, "reason": f"{tier}层预算上限"})
                continue
            max_by_cash = available_cash / (1 + COST)
            value = min(value, max_by_cash)
            trigger = float(sig["price"])
            shares = int(value // trigger)
            if shares <= 0:
                skipped.append({"ticker": ticker, "reason": "现金不足一股"})
                continue
            actual_budget = shares * trigger * (1 + COST)
            order = {
                "session_date": session,
                "signal_date": sig["date"],
                "ticker": ticker,
                "tier": tier,
                "trigger_price": _round2(trigger),
                "planned_shares": shares,
                "planned_amount_usd": _round2(shares * trigger),
                "rank": sig.get("rank"),
                "books": {k: (None if v is None else round(float(v), 1)) for k, v in sig["books"].items()},
                "certainty": sig.get("certainty"),
                "fund_score": sig.get("fund_score"),
                "status": "pending",
            }
            orders.append(order)
            projected_counts[tier] += 1
            projected_mv[tier] += shares * trigger
            available_cash -= actual_budget

    state["pending_buys"] = [o for o in state.get("pending_buys", []) if o.get("session_date") != session] + orders
    report = {
        "type": "premarket",
        "session_date": session,
        "signal_date": signal_date,
        "spy": spy,
        "equity_mark_usd": _round2(equity),
        "cash_usd": state["cash"],
        "positions": [
            {
                "ticker": p["ticker"], "tier": p["tier"], "shares": p["shares"],
                "entry_price": p["entry_price"], "last_price": p.get("last_price"),
                "stop_price": p["stop_price"], "take_price": p["take_price"],
                "take_profit_done": p.get("take_profit_done", False),
            }
            for p in state["positions"].values()
        ],
        "pending_liquidations": state["pending_liquidations"],
        "new_buy_orders": orders,
        "skipped": skipped,
        "signal_failures": failures,
        "note": "纸面跟踪；指定价为信号日收盘价，盘中触及才记成交。基本面死亡只生成下一交易日开盘退出单。",
    }
    save_state(state, state_path)
    path = write_report(f"premarket_{session}.json", report)
    return report, path


def _download_intraday(tickers, session):
    """返回 {ticker: DataFrame(index=纽约时间, open/high/low/close)}；优先1分钟，失败留空。"""
    import yfinance as yf

    start = session
    end = (date.fromisoformat(session) + timedelta(days=1)).isoformat()
    result = {}
    try:
        raw = yf.download(
            tickers=tickers, start=start, end=end, interval="1m", auto_adjust=False,
            prepost=False, progress=False, group_by="ticker", threads=True,
        )
    except Exception:
        return result
    if raw is None or len(raw) == 0:
        return result
    for ticker in tickers:
        try:
            df = raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
            df = df.dropna(how="all")
            if df.empty:
                continue
            df.index = pd.to_datetime(df.index)
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_convert("America/New_York").tz_localize(None)
            df = df[[c for c in ["Open", "High", "Low", "Close"] if c in df.columns]]
            df.columns = [c.lower() for c in df.columns]
            df = df[df.index.date == date.fromisoformat(session)]
            if not df.empty:
                result[ticker] = df.sort_index()
        except Exception:
            continue
    return result


def _download_daily(tickers, session):
    import yfinance as yf

    start = (date.fromisoformat(session) - timedelta(days=7)).isoformat()
    end = (date.fromisoformat(session) + timedelta(days=1)).isoformat()
    result = {}
    try:
        raw = yf.download(tickers=tickers, start=start, end=end, interval="1d", auto_adjust=False,
                          progress=False, group_by="ticker", threads=True)
    except Exception:
        return result
    if raw is None or len(raw) == 0:
        return result
    for ticker in tickers:
        try:
            df = raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex) else raw.copy()
            df.index = pd.to_datetime(df.index).date
            row = df.loc[date.fromisoformat(session)]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            result[ticker] = {k.lower(): float(row[k]) for k in ["Open", "High", "Low", "Close"]}
        except Exception:
            continue
    return result


def _bar_event(bar, stop_price, take_price, take_done):
    """单根K线内判断先碰哪条线；both 时用 OHLC 路径推断并标注。"""
    o, h, l, c = (float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]))
    if o <= stop_price:
        return ("stop", o, "开盘跳空至止损下方，按开盘价")
    if o >= take_price and not take_done:
        return ("take", o, "开盘跳空至止盈上方，按开盘价")
    hit_stop = l <= stop_price
    hit_take = (not take_done) and h >= take_price
    if hit_stop and hit_take:
        # 上涨K线常见路径 O->L->H->C；下跌K线常见路径 O->H->L->C。同分钟/同日双碰时据此排先后。
        if c >= o:
            return ("stop", stop_price, "双碰：按上涨K线路径推断先低后高，先止损")
        return ("take", take_price, "双碰：按下跌K线路径推断先高后低，先止盈")
    if hit_stop:
        return ("stop", stop_price, "盘中触及止损")
    if hit_take:
        return ("take", take_price, "盘中触及止盈")
    return (None, None, None)


def settle(state_path, session):
    state = load_state(state_path)
    if session in state.get("settled_sessions", []):
        report = {"type": "settle", "session_date": session, "status": "already_settled"}
        return report, write_report(f"settle_{session}.json", report)

    buys_today = [o for o in state.get("pending_buys", []) if o.get("session_date") == session]
    liquidations = {x["ticker"]: x for x in state.get("pending_liquidations", []) if x.get("session_date") == session}
    relevant = sorted(set(list(state["positions"].keys()) + [o["ticker"] for o in buys_today] + list(liquidations.keys())))
    intraday = _download_intraday(relevant, session)
    daily = _download_daily(relevant, session)
    events = []
    closes = {}

    # 每个标的构建按时间排序的K线序列；无分钟线时退化为一根日线。
    series = {}
    for ticker in relevant:
        if ticker in intraday:
            df = intraday[ticker]
            series[ticker] = [(ts, row) for ts, row in df.iterrows()]
            closes[ticker] = float(df.iloc[-1]["close"])
        elif ticker in daily:
            d = daily[ticker]
            ts = pd.Timestamp(session + " 09:30:00")
            series[ticker] = [(ts, d)]
            closes[ticker] = d["close"]

    all_times = sorted({ts for rows in series.values() for ts, _ in rows})
    buy_by_ticker = {o["ticker"]: o for o in buys_today}
    filled_buys = set()

    for ts in all_times:
        for ticker in relevant:
            rows = dict(series.get(ticker, []))
            if ts not in rows:
                continue
            bar = rows[ts]
            pos = state["positions"].get(ticker)

            # 基本面死亡退出：开盘价执行，且优先于止损/止盈。
            if pos and ticker in liquidations and ts == series[ticker][0][0]:
                price = float(bar["open"])
                qty = pos["shares"]
                proceeds = qty * price * (1 - COST)
                state["cash"] = _round2(state["cash"] + proceeds)
                events.append({
                    "time": str(ts), "ticker": ticker, "action": "fundamental_exit_sell_all",
                    "shares": qty, "price": _round2(price), "reason": liquidations[ticker]["reason"],
                })
                del state["positions"][ticker]
                pos = None

            if pos:
                # 新买入当日不在同一根K线立刻触发其止盈止损，从下一根K线起生效。
                entry_ts = pos.get("entry_timestamp")
                if not (pos.get("entry_date") == session and entry_ts and pd.Timestamp(entry_ts) >= ts):
                    kind, price, why = _bar_event(bar, pos["stop_price"], pos["take_price"], pos.get("take_profit_done", False))
                    if kind == "stop":
                        qty = pos["shares"]
                        proceeds = qty * float(price) * (1 - COST)
                        state["cash"] = _round2(state["cash"] + proceeds)
                        events.append({"time": str(ts), "ticker": ticker, "action": "stop_loss_sell_all",
                                       "shares": qty, "price": _round2(price), "reason": why})
                        del state["positions"][ticker]
                        pos = None
                    elif kind == "take":
                        qty = max(1, int(pos["shares"] // 3)) if pos["shares"] > 1 else 1
                        qty = min(qty, pos["shares"])
                        proceeds = qty * float(price) * (1 - COST)
                        state["cash"] = _round2(state["cash"] + proceeds)
                        pos["shares"] -= qty
                        pos["take_profit_done"] = True
                        events.append({"time": str(ts), "ticker": ticker, "action": "take_profit_sell_one_third",
                                       "shares": qty, "price": _round2(price), "reason": why})
                        if pos["shares"] <= 0:
                            del state["positions"][ticker]
                            pos = None

            # 盘前限价买单：开盘低于触发价按开盘价，否则盘中低点触及按触发价。
            order = buy_by_ticker.get(ticker)
            if order and ticker not in filled_buys and ticker not in state["positions"]:
                o, l = float(bar["open"]), float(bar["low"])
                fill = None
                if o <= order["trigger_price"]:
                    fill = o
                elif l <= order["trigger_price"]:
                    fill = order["trigger_price"]
                if fill is not None:
                    shares = int(order["planned_shares"])
                    max_shares = int((state["cash"] / (1 + COST)) // fill)
                    shares = min(shares, max_shares)
                    if shares > 0:
                        cost = shares * fill * (1 + COST)
                        state["cash"] = _round2(state["cash"] - cost)
                        lines = make_lines(order["tier"], fill)
                        state["positions"][ticker] = {
                            "ticker": ticker, "tier": order["tier"], "shares": shares,
                            "entry_price": _round2(fill), "entry_date": session,
                            "entry_timestamp": str(ts), "entry_note": "盘前限价触发买入",
                            "last_price": _round2(fill), **lines, "take_profit_done": False,
                        }
                        events.append({"time": str(ts), "ticker": ticker, "action": "buy",
                                       "shares": shares, "price": _round2(fill),
                                       "trigger_price": order["trigger_price"]})
                    filled_buys.add(ticker)

    for order in buys_today:
        order["status"] = "filled" if order["ticker"] in filled_buys else "expired_unfilled"
    state["pending_buys"] = [o for o in state.get("pending_buys", []) if o.get("session_date") != session]
    state["pending_liquidations"] = [x for x in state.get("pending_liquidations", []) if x.get("session_date") != session]
    for ticker, pos in state["positions"].items():
        if ticker in closes:
            pos["last_price"] = _round2(closes[ticker])
    state["history"].extend([{**e, "session_date": session} for e in events])
    state["settled_sessions"].append(session)
    state["settled_sessions"] = sorted(set(state["settled_sessions"]))

    equity = state["cash"] + sum(p["shares"] * float(p.get("last_price", p["entry_price"])) for p in state["positions"].values())
    report = {
        "type": "settle",
        "session_date": session,
        "events": events,
        "cash_usd": state["cash"],
        "equity_mark_usd": _round2(equity),
        "positions": list(state["positions"].values()),
        "expired_or_filled_buys": buys_today,
        "data_coverage": {"intraday_1m": sorted(intraday.keys()), "daily_only": sorted(set(daily) - set(intraday))},
        "note": "纸面结算；1分钟线优先。无分钟线标的按日线OHLC路径近似，报告会列在 daily_only。",
    }
    save_state(state, state_path)
    return report, write_report(f"settle_{session}.json", report)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "premarket", "settle"])
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--date", dest="session_date")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.cmd == "init":
        if os.path.exists(args.state) and not args.force:
            print(f"state already exists: {args.state} (use --force to overwrite)")
            return
        state = initial_state()
        save_state(state, args.state)
        print(json.dumps({"state": args.state, "cash": state["cash"], "positions": len(state["positions"])}, ensure_ascii=False))
        return
    if args.cmd == "premarket":
        report, path = premarket(args.state, args.session_date)
        print(json.dumps({"report": path, "session_date": report.get("session_date"), "new_buy_orders": len(report.get("new_buy_orders", []))}, ensure_ascii=False))
        return
    if not args.session_date:
        raise SystemExit("settle requires --date YYYY-MM-DD")
    report, path = settle(args.state, args.session_date)
    print(json.dumps({"report": path, "session_date": args.session_date, "events": len(report.get("events", [])), "equity_mark_usd": report.get("equity_mark_usd")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
