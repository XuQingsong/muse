"""
把今日纸面计划（today_plan_2026-10-08.json 的 10 笔）转成 IBKR 订单。

双保险设计：
  1. 默认只预览：连接（只读）→ 读账户净值 → 按 --equity 缩放算股数 → 打印订单表，不发单。
  2. 加 --execute 才真正发送，且发送前必须在终端手动输入 YES 确认；
     发送时用普通（非只读）连接——本地 IB Gateway 必须先取消勾选 "Read-Only API"，
     否则 IBKR 会拒单并报错，脚本会把错误原样打印出来。

用法（在 ~/workspace/quant-practice 本机运行，IB Gateway 已登录）：
  python3 place_orders.py                      # 预览（按纸账户净值自动缩放）
  python3 place_orders.py --equity 250000      # 以 25 万美元权益为缩放基准预览
  python3 place_orders.py --execute            # 真正发单（仍需手动输 YES）
口径：计划金额是按 100 万美元权益算出的美元数；股数 = floor(金额 × (实际权益/100万) / 计划参考价)。
参考价为信号日收盘价，仅用于估算股数；实发为市价单 DAY，以开盘实际成交为准。
"""
import argparse
import json
import sys
import time

PLAN_FILE = "today_plan_2026-10-08.json"
BASE_EQUITY = 1_000_000.0


def load_plan():
    d = json.load(open(PLAN_FILE))
    return [{"ticker": p["ticker"], "usd": p["buy_usd"], "ref": p["price"], "tier": p["tier"]}
            for p in d["plan"]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=4002, help="IB Gateway 端口，纸面 4002 / 实盘 4001")
    ap.add_argument("--equity", type=float, default=None, help="缩放基准权益（美元），缺省读账户 NetLiquidation")
    ap.add_argument("--execute", action="store_true", help="真正发送订单（缺省只预览）")
    args = ap.parse_args()

    try:
        from ib_async import IB, Stock, Order
    except ImportError:
        print("缺 ib_async 库。先运行： pip3 install ib_async")
        sys.exit(1)

    plan = load_plan()
    ib = IB()
    print(f"连接 127.0.0.1:{args.port}（只读）...")
    try:
        ib.connect("127.0.0.1", args.port, clientId=78, readonly=True, timeout=20)
    except Exception as e:
        print(f"连接失败：{type(e).__name__}: {e}")
        print("排查：Gateway 已登录；Settings > API 已开启；端口与纸面/实盘登录模式一致。")
        sys.exit(2)

    summary = {s.tag: s.value for s in ib.accountSummary()}
    netliq = float(summary.get("NetLiquidation", 0) or 0)
    equity = args.equity or netliq
    if equity <= 0:
        print("读不到账户净值，也未用 --equity 指定，终止。")
        sys.exit(3)
    scale = equity / BASE_EQUITY
    print(f"账户净值 NetLiquidation = {netliq:,.0f} 美元；缩放基准权益 = {equity:,.0f} 美元（比例 {scale:.3f}）")

    orders = []
    total = 0.0
    print("\n— 订单预览（信号日 2026-10-07 收盘价估算） —")
    print(f"{'标的':>6} {'层':>4} {'目标金额$':>10} {'参考价$':>9} {'股数':>7} {'估算金额$':>11}")
    for p in plan:
        usd = p["usd"] * scale
        qty = int(usd // p["ref"])
        est = qty * p["ref"]
        if qty <= 0:
            print(f"{p['ticker']:>6} {p['tier']:>4} {usd:>10,.0f} {p['ref']:>9.2f} {qty:>7} —— 金额不足 1 股，跳过")
            continue
        orders.append((p["ticker"], qty))
        total += est
        print(f"{p['ticker']:>6} {p['tier']:>4} {usd:>10,.0f} {p['ref']:>9.2f} {qty:>7} {est:>11,.0f}")
    print(f"合计估算 {total:,.0f} 美元，占权益 {total / equity * 100:.1f}%")

    cash = float(summary.get("TotalCashValue", 0) or 0)
    if total > equity * 0.98:
        print("警告：订单总额超过权益 98%，请先用 --equity 降低缩放基准再跑。")
        ib.disconnect()
        sys.exit(4)

    if not args.execute:
        print("\n[预览模式] 未发送任何订单。确认无误后：去掉 Gateway 的 Read-Only API 勾选，再加 --execute 运行。")
        ib.disconnect()
        return

    ib.disconnect()
    print("\n以可写连接重新连接（发送订单）...")
    ib = IB()
    ib.connect("127.0.0.1", args.port, clientId=79, readonly=False, timeout=20)
    ans = input(f"将发送 {len(orders)} 笔市价买单（DAY），估算总额 {total:,.0f} 美元。确认发送请输入 YES：")
    if ans.strip() != "YES":
        print("未确认，已取消，未发送任何订单。")
        ib.disconnect()
        return

    trades_log = []
    for ticker, qty in orders:
        contract = Stock(ticker, "SMART", "USD")
        ib.qualifyContracts(contract)
        order = Order(action="BUY", orderType="MKT", totalQuantity=qty, tif="DAY")
        trade = ib.placeOrder(contract, order)
        trades_log.append((ticker, qty, trade))
        print(f"已发送：{ticker} 买 {qty} 股（市价/DAY），orderId={trade.order.orderId}")

    print("等待成交回报（最多 30 秒）...")
    for _ in range(30):
        ib.sleep(1)
        if all(t.isDone() for _, _, t in trades_log):
            break
    print("\n— 执行结果 —")
    for ticker, qty, trade in trades_log:
        st = trade.orderStatus
        print(f"{ticker}: 状态 {st.status}，已成 {st.filled}/{qty}，均价 {st.avgFillPrice}")
    log = [{"ticker": tkr, "qty": q, "status": tr.orderStatus.status,
            "filled": tr.orderStatus.filled, "avgFillPrice": tr.orderStatus.avgFillPrice}
           for tkr, q, tr in trades_log]
    fn = f"ibkr_orders_{time.strftime('%Y%m%d_%H%M%S')}.json"
    json.dump(log, open(fn, "w"), ensure_ascii=False, indent=1)
    print(f"执行日志已保存：{fn}")
    ib.disconnect()


if __name__ == "__main__":
    main()
