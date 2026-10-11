"""
IB Gateway 只读连通性验证脚本（在本机 Mac 上运行）。
前提：IB Gateway 已启动并登录，API 已开启（纸面端口 4002 / 实盘 4001）。
本脚本只读：拉账户摘要、持仓、最近成交；不下单、不改单、不撤单。
用法：python3 test_ibkr.py [port]   例：python3 test_ibkr.py 4002
"""
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 4002
HOST = "127.0.0.1"
CLIENT_ID = 77


def main():
    try:
        from ib_async import IB
    except ImportError:
        print("缺 ib_async 库。先运行： pip install ib_async")
        sys.exit(1)

    ib = IB()
    print(f"连接 {HOST}:{PORT} ...")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=20)
    except Exception as e:
        print(f"连接失败：{type(e).__name__}: {e}")
        print("排查：1) Gateway 已启动并完成登录（含 2FA）；2) Settings > API > Settings 已勾 Enable ActiveX and Socket Clients；"
              "3) 端口与登录模式一致（纸面 4002 / 实盘 4001）。")
        sys.exit(2)

    print(f"已连接。服务器版本 {ib.client.serverVersion()}，账户：{ib.managedAccounts()}")

    summary = ib.accountSummary()
    keep = {"NetLiquidation", "TotalCashValue", "GrossPositionValue", "AvailableFunds", "BuyingPower", "Currency"}
    print("\n— 账户摘要 —")
    for s in summary:
        if s.tag in keep:
            print(f"  {s.tag}: {s.value} {s.currency}")

    print("\n— 持仓 —")
    portfolio = ib.portfolio()
    if not portfolio:
        print("  （无持仓）")
    for p in portfolio:
        c = p.contract
        print(f"  {c.symbol:>6} {c.secType} 数量 {p.position} 均价 {p.averageCost:.4f} "
              f"市值 {p.marketValue:.2f} 未实现盈亏 {p.unrealizedPNL:.2f}")

    print("\n— 最近成交 —")
    fills = ib.fills()
    if not fills:
        print("  （本次会话无成交回报；属正常，Gateway 重启后为空）")
    for f in fills[:20]:
        ex = f.execution
        print(f"  {ex.time} {f.contract.symbol} {ex.side} {ex.shares} @ {ex.price}")

    ib.disconnect()
    print("\n验证完成，已断开。（本脚本只读，未发送任何订单）")


if __name__ == "__main__":
    main()
