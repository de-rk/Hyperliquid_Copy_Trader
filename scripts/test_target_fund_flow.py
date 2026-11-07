"""Standalone checks for target-wallet fund-flow detection (no network).

Run:  .venv/bin/python scripts/test_target_fund_flow.py

Covers the pure decision logic, the exchange ledger parser, and one full poll
tick with a stub client/notifier. No orders are ever placed.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
import telegram_bot.notifications as notifications  # noqa: E402
from utils.equity_flow import (  # noqa: E402
    EquitySnapshot,
    analyze_equity_flow,
    ledger_lines,
    summarize_ledger_updates,
)

PASS = "✅"
FAIL = "❌"
results = []

WALLET = "0x62f8aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OTHER = "0x9999bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

CFG = dict(min_usd=100.0, min_pct=1.0, max_position_change_pct=5.0)


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


def snap(equity, upnl, positions=(), wallet=WALLET, ms=1_000):
    return EquitySnapshot(wallet, equity, upnl, tuple(positions), ms)


def analyze(prev, cur, **overrides):
    return analyze_equity_flow(prev, cur, **(CFG | overrides))


# --------------------------------------------------------------- pure logic
flat = snap(10_000, 0, [("BTC", 0.5)], ms=1_000)
inflow = snap(11_000, 0, [("BTC", 0.5)], ms=61_000)
flow = analyze(flat, inflow)
check("仓位不变 + 净值上升 → 判定转入", flow is not None and flow.direction == "in", str(flow))
check("转入金额 = 净值变化", flow and abs(flow.net_flow - 1_000) < 1e-6, str(flow))
check("仓位变化率为 0", flow and flow.position_change_pct == 0.0, str(flow))

price_only = snap(11_000, 1_000, [("BTC", 0.5)], ms=61_000)
check("纯浮盈上涨不误报入金", analyze(flat, price_only) is None)

partly_pnl = snap(11_020, 20, [("BTC", 0.5)], ms=61_000)
f2 = analyze(flat, partly_pnl)
check("扣除浮盈后的净额才上报", f2 and abs(f2.net_flow - 1_000) < 1e-6, str(f2))
check("通知里保留原始浮盈变化", f2 and abs(f2.unrealized_pnl_delta - 20) < 1e-6, str(f2))

outflow = snap(9_180, 20, [("BTC", 0.5)], ms=61_000)
f3 = analyze(flat, outflow)
check("净值下降 → 判定转出", f3 is not None and f3.direction == "out", str(f3))
check("转出金额 = 净值变化 − 浮盈变化", f3 and abs(f3.net_flow + 840) < 1e-6, str(f3))

check("仓位翻倍 → 不判定出入金", analyze(flat, snap(11_000, 0, [("BTC", 1.0)])) is None)
check("多转空（带方向数量）→ 不判定", analyze(flat, snap(11_000, 0, [("BTC", -0.5)])) is None)
check("仓位仍在容忍度内 → 判定", analyze(flat, snap(11_000, 0, [("BTC", 0.51)])) is not None)
check("小额变动 → 不判定", analyze(flat, snap(10_050, 0, [("BTC", 0.5)])) is None)
check("大额但比例不足 → 不判定", analyze(snap(1_000_000, 0), snap(1_000_150, 0)) is None)
check("钱包切换 → 不判定", analyze(flat, snap(11_000, 0, wallet=OTHER)) is None)
check("读到 0 净值 → 不判定", analyze(flat, snap(0, 0)) is None)
check("读到负净值 → 不判定", analyze(flat, snap(-100, 0)) is None)
check("空仓 → 空仓 + 入金 → 判定", analyze(snap(1_000, 0), snap(2_000, 0)) is not None)
check("空仓 → 开仓 → 不判定", analyze(snap(1_000, 0), snap(2_000, 0, [("BTC", 0.5)])) is None)
check("价格变化不改仓位变化率（用 size 而非名义额）",
      analyze(flat, snap(10_500, 500, [("BTC", 0.5)])) is None)

# ------------------------------------------------------------- ledger parse
now_ms = int(time.time() * 1000)
lo, hi = now_ms - 120_000, now_ms + 120_000
led = summarize_ledger_updates(
    [
        {"time": now_ms, "delta": {"type": "deposit", "usdc": "1000"}},
        {"time": now_ms, "delta": {"type": "withdraw", "usdc": "300", "fee": "1"}},
        {"time": now_ms - 500_000, "delta": {"type": "deposit", "usdc": "99999"}},
        {"time": now_ms, "delta": {"type": "internalTransfer", "usdc": "50", "destination": WALLET}},
        {"time": now_ms, "delta": {"type": "spotTransfer", "token": "USDE", "amount": "0.0025", "usdcValue": "999.99", "destination": WALLET}},
        {"time": now_ms, "delta": {"type": "spotTransfer", "token": "USDC", "amount": "12", "usdcValue": "12", "destination": WALLET}},
        {"time": now_ms, "delta": {"type": "weirdFutureType", "what": "ever"}},
        "garbage",
    ],
    WALLET, start_ms=lo, end_ms=hi,
)
check("流水：充值金额", abs(led.deposits - 1_000) < 1e-6, str(led))
check("流水：提现含手续费", abs(led.withdrawals - 301) < 1e-6, str(led))
check("流水：链上净额", abs(led.chain_net - 699) < 1e-6, str(led))
check("流水：窗口外事件被过滤", led.deposit_count == 1, str(led))
check("流水：转入型内部划记为正", abs(led.other_net - 62) < 1e-6, str(led))
check("流水：非 USDC 现货估值不计入金额",
      dict(led.other_counts).get("spotTransfer") == 2, str(led))
check("流水：USDC 现货转账按实际金额计入",
      abs(led.other_net - 62) < 1e-6, str(led))
check("流水：未知类型只计数不编造金额", dict(led.other_counts).get("weirdFutureType") == 1, str(led))
check("流水：matched 为真", led.matched is True)
check("流水文案包含充值与提现", "充值" in ledger_lines(led) and "提现" in ledger_lines(led))
empty_led = summarize_ledger_updates([], WALLET, start_ms=lo, end_ms=hi)
check("空窗口文案", "无充提" in ledger_lines(empty_led), ledger_lines(empty_led))
failed_led = summarize_ledger_updates([], WALLET, start_ms=lo, end_ms=hi, ok=False, error="timeout")
check("流水失败文案", "失败" in ledger_lines(failed_led), ledger_lines(failed_led))

# Same wallet on both sides of a `send` = a spot/perp book move, not cash flow.
perp_in = summarize_ledger_updates(
    [{"time": now_ms, "delta": {
        "type": "send", "user": WALLET, "destination": WALLET,
        "token": "USDC", "amount": "283.86",
        "sourceDex": "spot", "destinationDex": "",
    }}],
    WALLET, start_ms=lo, end_ms=hi,
)
check("现货→合约的内部划转不记入金", abs(perp_in.other_net) < 1e-9, str(perp_in))
check("内部划转记入 internal_net", abs(perp_in.internal_net - 283.86) < 1e-6, str(perp_in))
check("内部划转可解释净值跳变", perp_in.explains_internal(283.86) is True)
check("有真实充提时不认内部划转", led.explains_internal(1_000) is False)
check("文案标明内部划转", "内部划转" in ledger_lines(perp_in), ledger_lines(perp_in))

# Real rows from 0xf8bf…391c: a 281.80 spot->bridge send that came straight
# back, then a 280.80+1.00 withdrawal. The old code counted the outbound leg
# as an inflow and reported +3,150 of phantom deposits over 30 days.
real_rows = [
    {"time": now_ms, "delta": {"type": "send", "user": WALLET, "destination": WALLET,
                               "token": "USDC", "amount": "283.86",
                               "sourceDex": "spot", "destinationDex": "xyz"}},
    {"time": now_ms, "delta": {"type": "send", "user": WALLET, "destination": OTHER,
                               "token": "USDC", "amount": "281.800537",
                               "sourceDex": "spot", "destinationDex": "spot"}},
    {"time": now_ms, "delta": {"type": "spotTransfer", "token": "USDC", "amount": "281.800537",
                               "usdcValue": "281.800537", "user": OTHER,
                               "destination": WALLET}},
    {"time": now_ms, "delta": {"type": "withdraw", "usdc": "280.802675", "fee": "1.0"}},
]
real_led = summarize_ledger_updates(real_rows, WALLET, start_ms=lo, end_ms=hi)
check("真实数据：提现净额 −281.80",
      abs(real_led.chain_net + 281.802675) < 1e-6, str(real_led))
check("真实数据：内在双列划转不外报",
      abs(real_led.other_net) < 1e-6, str(real_led))
check("真实数据：无虚假入金", abs(real_led.other_net) < 1e-6 and real_led.deposits == 0, str(real_led))


# ------------------------------------------------------------------ poll tick
class StubClient:
    def __init__(self, states, ledger=None, ledger_error=None, portfolio=11_000.0):
        self.states = list(states)
        self.ledger = ledger or []
        self.ledger_error = ledger_error
        self.portfolio = portfolio
        self.state_calls = 0
        self.ledger_calls = 0

    async def get_user_state(self, wallet):
        self.state_calls += 1
        return self.states.pop(0) if self.states else None

    async def get_non_funding_ledger_updates(self, wallet, start_ms, end_ms):
        self.ledger_calls += 1
        if self.ledger_error:
            raise RuntimeError(self.ledger_error)
        return self.ledger

    async def get_portfolio_account_value(self, wallet):
        return self.portfolio


class StubNotifier:
    def __init__(self):
        self.calls = []

    async def send_target_fund_flow_notification(self, **kwargs):
        self.calls.append(kwargs)
        return True


def state(equity, upnl, positions=()):
    return SimpleNamespace(
        balance=equity,
        unrealized_pnl=upnl,
        positions=[
            SimpleNamespace(symbol=s, size=abs(n), side=SimpleNamespace(value="long" if n >= 0 else "short"))
            for s, n in positions
        ],
    )


def reset(client=None, notifier=None, cooldown=0, wallet=WALLET):
    main.client = client
    main.notifier = notifier
    main.last_target_equity_snapshot = None
    main.target_flow_next_alert_at = 0.0
    main.target_flow_last_check_at = None
    main.target_flow_last_equity = None
    main.settings.target_wallet = wallet
    cfg = main.settings.target_equity_flow
    cfg.enabled = True
    cfg.min_usd = 100.0
    cfg.min_pct = 1.0
    cfg.max_position_change_pct = 5.0
    cfg.cooldown_seconds = cooldown


async def scenario(name, states, *, cooldown=0, wallet=WALLET, ledger=None,
                   ledger_error=None, extra_wallet=None):
    client = StubClient(states, ledger=ledger, ledger_error=ledger_error)
    notifier = StubNotifier()
    reset(client, notifier, cooldown, wallet)
    for _ in states:
        if extra_wallet:
            main.settings.target_wallet = extra_wallet.pop(0)
        await main.check_target_fund_flow_once()
    return client, notifier.calls


async def run_tick_checks():
    dep = [{"time": int(time.time() * 1000), "delta": {"type": "deposit", "usdc": "1000"}}]

    _, calls = await scenario("首次采样", [state(10_000, 0, [("BTC", 0.5)])])
    check("首次采样只建基线，不发通知", len(calls) == 0, str(calls))
    check("首次采样记录净值供 /status", main.target_flow_last_equity == 10_000)

    _, calls = await scenario(
        "转入", [state(10_000, 0, [("BTC", 0.5)]), state(11_000, 0, [("BTC", 0.5)])],
        ledger=dep,
    )
    check("转入触发一条通知", len(calls) == 1, str(calls))
    if calls:
        c = calls[0]
        check("通知方向为转入", c["direction"] == "in", str(c))
        check("通知金额 $1,000", abs(c["net_flow"] - 1000) < 1e-6, str(c))
        check("通知含链上流水", "充值 +$1,000.00" in c["ledger_text"], str(c["ledger_text"]))
        check("通知含全账户净值", c["portfolio_equity"] == 11_000.0, str(c))
        check("通知 chain_matched", c["chain_matched"] is True)
    check("真实通知模板渲染成功", await render_notification(calls[0]) if calls else False)

    _, calls = await scenario(
        "重复", [state(10_000, 0), state(11_000, 0), state(11_000, 0)], ledger=dep,
    )
    check("同一笔转账不会重复通知", len(calls) == 1, str(calls))

    _, calls = await scenario(
        "冷静期",
        [state(10_000, 0), state(11_000, 0), state(12_000, 0)],
        cooldown=9999, ledger=dep,
    )
    check("冷静期内第二次转账被抑制", len(calls) == 1, str(calls))

    for label, bad in (("读取失败", None), ("零净值", state(0, 0))):
        client = StubClient([state(10_000, 0), bad, state(11_000, 0)], ledger=dep)
        notifier = StubNotifier()
        reset(client, notifier)
        await main.check_target_fund_flow_once()
        await main.check_target_fund_flow_once()
        check(f"{label}那一轮不发通知", len(notifier.calls) == 0, str(notifier.calls))
        check(f"{label}不推进基线", main.target_flow_last_equity == 10_000)
        await main.check_target_fund_flow_once()
        check(f"{label}恢复后仍能检出期间的转账", len(notifier.calls) == 1, str(notifier.calls))

    _, calls = await scenario(
        "切换目标", [state(10_000, 0), state(20_000, 0)],
        extra_wallet=[WALLET, OTHER], ledger=dep,
    )
    check("/follow 切换目标重建基线，零通知", len(calls) == 0, str(calls))

    _, calls = await scenario(
        "行情误报", [state(10_000, 0), state(11_000, 1_000)], ledger=dep,
    )
    check("浮盈导致的净值跳变不发通知", len(calls) == 0, str(calls))

    _, calls = await scenario(
        "仓位变动", [state(10_000, 0, [("BTC", 0.5)]), state(11_000, 0, [("BTC", 1.0)])],
        ledger=dep,
    )
    check("仓位大变时不发通知", len(calls) == 0, str(calls))

    _, calls = await scenario(
        "内部划转",
        [state(10_000, 0), state(11_000, 0)],
        ledger=[{"time": int(time.time() * 1000), "delta": {
            "type": "send", "user": WALLET, "destination": WALLET,
            "token": "USDC", "amount": "1000",
            "sourceDex": "spot", "destinationDex": "",
        }}],
    )
    check("目标把现货划进合约时不误报入金", len(calls) == 0, str(calls))

    _, calls = await scenario(
        "流水失败", [state(10_000, 0), state(9_000, 0)], ledger_error="timeout",
    )
    check("流水查询失败仍发出推断通知", len(calls) == 1, str(calls))
    if calls:
        check("通知注明流水失败", "流水查询失败" in calls[0]["ledger_text"], calls[0]["ledger_text"])
        check("净流出方向正确", calls[0]["direction"] == "out", str(calls[0]))


async def render_notification(payload):
    """Render the real HTML template with the values from a detected flow."""
    sent = {}

    class FakeBot:
        async def send_message(self, chat_id, text, parse_mode=None):
            sent["text"] = text
            return True

    svc = notifications.NotificationService.__new__(notifications.NotificationService)
    svc.bot = FakeBot()
    svc.chat_id = "1"
    svc.enabled = True
    ok = await svc.send_target_fund_flow_notification(**payload)
    text = sent.get("text", "")
    check("通知含方向标题", "转入" in text, text)
    check("通知含推断金额", "$1,000.00" in text, text)
    check("通知含仓位变化标签", "基本未变" in text, text)
    check("通知含跟单金额提示", "跟单金额" in text, text)
    return ok and all(k in text for k in ("转入", "$1,000.00", "基本未变"))


asyncio.run(run_tick_checks())

failed = [name for name, ok, _ in results if not ok]
print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
if failed:
    print("失败项：", "、".join(failed))
    sys.exit(1)
