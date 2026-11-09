"""Checks for the /copyloss preview estimate and receipt arithmetic.

The preview used to quote pure proportional sizing (target notional × equity
ratio), while the order is additionally capped by the free margin of the
symbol's own perp DEX and by MAX_POSITION_SIZE. On a HIP-3 symbol those two
numbers differ by several times, so the preview has to show the executable
figure — and the receipt has to quote the notional that actually settled.

Run:  .venv/bin/python scripts/test_copyloss_preview.py
"""
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from hyperliquid.models import Position, PositionSide  # noqa: E402

PASS = "✅"
FAIL = "❌"
results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


class StubClient:
    """Per-DEX available margin, keyed by the ``dex`` argument."""

    def __init__(self, available: dict, abstraction=None, available_to_trade=None):
        self.available = available
        self.abstraction = abstraction
        self.available_to_trade = available_to_trade or {}
        self.abstraction_calls = 0
        self.available_to_trade_calls = 0

    async def get_user_state(self, address, dex=None):
        if dex not in self.available:
            return None
        value = self.available[dex]
        if value is None:
            return None
        return SimpleNamespace(
            available_balance=value,
            balance=value,
            margin_used=0.0,
            positions=[],
            orders=[],
            timestamp=datetime.now(),
        )

    async def get_account_abstraction(self, address):
        self.abstraction_calls += 1
        return self.abstraction

    async def get_available_to_trade_margin(self, address, symbol, side, target_leverage=None):
        self.available_to_trade_calls += 1
        return self.available_to_trade.get(symbol)


def position(symbol="xyz:SKHX", size=1821.0, entry=862.145, leverage=10):
    return Position(
        symbol=symbol,
        side=PositionSide.SHORT,
        size=size,
        entry_price=entry,
        current_price=828.36,
        leverage=leverage,
        unrealized_pnl=-8097.0,
    )


def configure(*, simulated=False, balance=0.0, usage=0.40, cap=None, adjustment=1.0):
    main.settings.simulated_trading = simulated
    main.settings.hyperliquid.wallet_address = "0xf8bff73b4349e5cb106b216994af52094412391c"
    main.settings.hyperliquid.account_mode = "standard"
    main.settings.leverage.adjustment_ratio = adjustment
    main.settings.copy_rules.max_margin_usage_ratio = usage
    main.settings.sizing.max_position_size = cap
    main.simulated_balance = balance
    main._account_mode_cache.update({"mode": None, "checked_at": 0.0})


# Real numbers from the 2026-11-09 10:56 copy: target 1,570,000 notional,
# follower portfolio $141.34, only $33.57 of it free on the xyz DEX.
RATIO = 141.34 / 603042.36


async def test_hip3_capped_by_dex_margin():
    configure()
    main.client = StubClient({"xyz": 33.5675})
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("预览保留比例金额", abs(plan["requested"] - 367.97) < 0.05, str(plan["requested"]))
    check("实下金额按该 DEX 保证金上限", abs(plan["executable"] - 134.27) < 0.05, str(plan["executable"]))
    check("差额被标记为缩量", plan["capped"] is True)
    check("说明里带 dex 名", plan["dex"] == "xyz", plan["dex"])
    check("说明里带可用保证金", "$33.57" in plan["basis"], plan["basis"])
    check("说明里带杠杆与占比", "10x" in plan["basis"] and "40%" in plan["basis"], plan["basis"])


async def test_same_dex_budget_is_shared():
    configure()
    main.client = StubClient({"xyz": 33.5675})
    two = await main.estimate_copy_execution(
        [position(symbol="xyz:AAA"), position(symbol="xyz:BBB")], RATIO
    )
    check("同 DEX 第一笔吃满单笔上限",
          abs(two["xyz:AAA"]["executable"] - 134.27) < 0.05, str(two["xyz:AAA"]["executable"]))
    second = two["xyz:BBB"]["executable"]
    expected = (33.5675 - 134.27 / 10) * 10 * 0.40
    check("同 DEX 第二笔按剩余保证金缩量", abs(second - expected) < 0.05, f"{second} vs {expected}")
    check("第二笔仍被标记缩量", two["xyz:BBB"]["capped"] is True)


async def test_position_cap_and_other_dex():
    configure(cap=50.0)
    main.client = StubClient({"xyz": 33.5675})
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("MAX_POSITION_SIZE 生效", abs(plan["executable"] - 50.0) < 1e-9, str(plan["executable"]))

    configure()
    main.client = StubClient({"": 10_000.0})
    btc = position(symbol="BTC", size=2.0, entry=50_000.0, leverage=10)
    plan = (await main.estimate_copy_execution([btc], RATIO))["BTC"]
    check("主账户符号用主账户保证金", plan["dex"] == "", plan["dex"])
    check("主账户额度充足时不缩量", plan["capped"] is False, str(plan["executable"]))


async def test_unreadable_state_is_flagged():
    configure()
    main.client = StubClient({})
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("读不到保证金时退回比例估算", abs(plan["executable"] - plan["requested"]) < 1e-9)
    check("并标记为未知，不假装准确", plan["unknown"] is True)


async def test_simulated_cap():
    configure(simulated=True, balance=10.0)
    main.client = StubClient({"xyz": 33.5675})
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("模拟模式按模拟余额算上限", abs(plan["executable"] - 10.0 * 10 * 0.40) < 1e-9, str(plan["executable"]))

    configure(simulated=True, balance=10_000.0)
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("模拟余额充足时不下调", plan["capped"] is False, str(plan["executable"]))


async def test_leverage_adjustment_moves_the_cap():
    configure(adjustment=0.5)
    main.client = StubClient({"xyz": 33.5675})
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("杠杆按比例下调", plan["leverage"] == 5, str(plan["leverage"]))
    check("上限随杠杆减半", abs(plan["executable"] - 33.5675 * 5 * 0.40) < 0.05, str(plan["executable"]))


def test_receipt_uses_fill_numbers():
    record = {
        "symbol": "xyz:SKHX",
        "side": "SHORT",
        "ok": True,
        "blocked": False,
        "reason": "",
        "size": 0.155149,
        "notional": 134.27,
        "requested_notional": 367.95,
        "leverage": 10,
        "order_id": "598995892945",
        "fill_price": 828.36,
        "fill_size": 0.155149,
    }
    text = main._copy_receipt_text([record], "浮亏", simulated=False)
    check("回执按成交价重算名义", "$128.52" in text, text.replace("\n", " | "))
    check("回执显示成交价", "@ $828.3600" in text, text.replace("\n", " | "))
    check("回执合计同步", "成交名义 $128.52" in text, text.replace("\n", " | "))

    missing = dict(record, fill_price=None, fill_size=None)
    text = main._copy_receipt_text([missing], "浮亏", simulated=False)
    check("没有成交价时退回下单金额", "$134.27" in text, text.replace("\n", " | "))
    check("并说明未取到成交价", "未取到成交价" in text)


async def test_unified_account_ignores_dex_sub_account():
    """The reported bug: config said standard while the wallet was unified."""
    configure()  # env/flag still says standard
    main.client = StubClient(
        {"xyz": 33.5675},
        abstraction="unifiedAccount",
        available_to_trade={"xyz:SKHX": 123.26},
    )
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("统一账户按全账户额度计算", abs(plan["available"] - 123.26) < 1e-6, str(plan["available"]))
    check("不再被 $33.57 子账户余额卡住", abs(plan["executable"] - 367.97) < 0.05, str(plan["executable"]))
    check("不再标记缩量", plan["capped"] is False)
    check("说明改为可交易额度", plan["basis"].startswith("可交易额度"), plan["basis"])
    check("没有去读 DEX 子账户", main.client.available_to_trade_calls == 1)


async def test_portfolio_margin_counts_as_unified():
    configure()
    main.client = StubClient(
        {"xyz": 33.5675},
        abstraction="portfolioMargin",
        available_to_trade={"xyz:SKHX": 20.0},
    )
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("组合保证金账户也走全账户额度",
          abs(plan["executable"] - 20.0 * 10 * 0.40) < 0.05, str(plan["executable"]))
    check("额度不足时仍标记缩量", plan["capped"] is True)


async def test_standard_and_unknown_modes_fall_back_to_config():
    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction="default")
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("default 账户仍按 DEX 保证金", abs(plan["executable"] - 134.27) < 0.05, str(plan["executable"]))

    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction="somethingNew")
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("未知模式退回配置值", abs(plan["executable"] - 134.27) < 0.05, str(plan["executable"]))

    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction=None)
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("接口读不到时退回配置值", abs(plan["executable"] - 134.27) < 0.05, str(plan["executable"]))


async def test_account_mode_is_cached():
    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction="unifiedAccount",
                             available_to_trade={"xyz:SKHX": 123.26})
    await main.resolve_account_mode()
    await main.resolve_account_mode()
    await main.resolve_account_mode()
    check("账户模式查询被缓存", main.client.abstraction_calls == 1, str(main.client.abstraction_calls))

    await main.resolve_account_mode(force=True)
    check("force 会重新查询", main.client.abstraction_calls == 2, str(main.client.abstraction_calls))

    check("abstraction 映射 unifiedAccount", main.account_mode_from_abstraction("unifiedAccount") == "unified")
    check("abstraction 映射 portfolioMargin", main.account_mode_from_abstraction("portfolioMargin") == "unified")
    check("abstraction 映射 default", main.account_mode_from_abstraction("default") == "standard")
    check("abstraction 空值返回 None", main.account_mode_from_abstraction("") is None)


async def test_live_cap_matches_preview_in_unified_mode():
    """The live cap has to agree with the preview, or the receipt drifts again."""
    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction="unifiedAccount",
                             available_to_trade={"xyz:SKHX": 123.26})
    cap = await main.get_follower_order_notional_cap(
        "xyz:SKHX", main.OrderSide.SELL, 10, prepare_leverage=False
    )
    check("实盘上限 = 保证金 × 杠杆 × 占比",
          abs(cap - 123.26 * 10 * 0.40) < 0.05, str(cap))
    plan = (await main.estimate_copy_execution([position()], RATIO))["xyz:SKHX"]
    check("预览不超过实盘上限", plan["executable"] <= cap + 0.05,
          f"{plan['executable']} vs {cap}")
    check("额度足够时预览不缩量", plan["capped"] is False)


async def test_unified_budget_is_spent_as_margin():
    configure()
    main.client = StubClient({"xyz": 33.5675}, abstraction="unifiedAccount",
                             available_to_trade={"xyz:AAA": 30.0, "xyz:BBB": 30.0})
    two = await main.estimate_copy_execution(
        [position(symbol="xyz:AAA"), position(symbol="xyz:BBB")], RATIO
    )
    first = two["xyz:AAA"]["executable"]
    check("第一笔吃满额度", abs(first - 30.0 * 10 * 0.40) < 0.05, str(first))
    expected = (30.0 - first / 10) * 10 * 0.40
    second = two["xyz:BBB"]["executable"]
    check("第二笔按剩余保证金缩量", abs(second - expected) < 0.05, f"{second} vs {expected}")


async def run():
    await test_hip3_capped_by_dex_margin()
    await test_same_dex_budget_is_shared()
    await test_position_cap_and_other_dex()
    await test_unreadable_state_is_flagged()
    await test_simulated_cap()
    await test_leverage_adjustment_moves_the_cap()
    await test_unified_account_ignores_dex_sub_account()
    await test_portfolio_margin_counts_as_unified()
    await test_standard_and_unknown_modes_fall_back_to_config()
    await test_account_mode_is_cached()
    await test_live_cap_matches_preview_in_unified_mode()
    await test_unified_budget_is_spent_as_margin()
    test_receipt_uses_fill_numbers()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
