"""Standalone checks for the /copyloss candidate scope.

The command used to list the target's losing positions and merely *mark* the
ones already held. It now lists only what the follower is missing: a symbol
already on the follower's book never reaches the numbered list, an opposite-side
position is reported but never offered, and ``force`` re-admits only the
same-side case.

Run:  .venv/bin/python scripts/test_copyloss_scope.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from hyperliquid.models import Position, PositionSide  # noqa: E402

PASS = "✅"
FAIL = "❌"
results = []

TARGET_EQUITY = 100_000.0


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(
        f"{PASS if cond else FAIL} {name}"
        + (f"  -- {detail}" if detail and not cond else "")
    )


class StubClient:
    """Target equity is fixed; the abstraction probe answers ``default``."""

    async def get_portfolio_account_value(self, address):
        return TARGET_EQUITY

    async def get_account_abstraction(self, address):
        return "default"


class StubMonitor:
    def __init__(self, positions):
        self.state = SimpleNamespace(
            positions=list(positions), unrealized_pnl=0.0, orders=[]
        )

    async def get_current_state(self):
        return self.state


class StubExecutor:
    """Only the tick size, which is all the minimum-order check reads."""

    def __init__(self, decimals=None):
        self.decimals = decimals or {}

    async def get_asset(self, symbol):
        value = self.decimals.get(symbol)
        return None if value is None else SimpleNamespace(sz_decimals=value)


def position(symbol, side, size, entry, leverage=10, pnl=0.0):
    return Position(
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry,
        current_price=entry,
        leverage=leverage,
        unrealized_pnl=pnl,
    )


def numbered_rows(text: str) -> list[str]:
    """The numbered candidate block only, without the skip footnotes."""
    return [line for line in text.splitlines() if line[:2].rstrip(".").isdigit()]


def configure(target_positions, held, *, balance=1_000.0, decimals=None):
    main.settings.simulated_trading = True
    main.settings.hyperliquid.wallet_address = "0xfollower00000000000000000000000000000000"
    main.settings.target_wallet = "0xtarget0000000000000000000000000000000000"
    main.settings.leverage.adjustment_ratio = 1.0
    main.settings.copy_rules.max_margin_usage_ratio = 0.40
    main.settings.sizing.max_position_size = None
    main.settings.copy_rules.copy_duplicate_guard_minutes = 0
    main.is_stopped = False
    main.is_paused = False
    main.wallet_unfunded = False
    main.simulated_balance = balance
    main.simulated_positions = {
        symbol: {"side": side.lower(), "size": size, "entry_price": entry}
        for symbol, (side, size, entry) in held.items()
    }
    main.client = StubClient()
    main.monitor = StubMonitor(target_positions)
    main.executor = StubExecutor(decimals)


BOARD = [
    position("BTC", PositionSide.LONG, 0.10, 70_000.0, pnl=-100.0),
    position("ETH", PositionSide.SHORT, 2.0, 3_000.0, pnl=40.0),
    position("xyz:SKHX", PositionSide.SHORT, 100.0, 800.0, pnl=-500.0),
]


async def test_held_same_side_falls_out_of_the_list():
    configure(BOARD, {"BTC": ("LONG", 0.05, 69_000.0)})
    text = await main.handle_copy_existing()
    check("已同向持有不占编号", "\n1. ETH SHORT" in text, text)
    check("已同向持有不进列表", "1. BTC LONG" not in text and "2. BTC LONG" not in text, text)
    check("底部点名跳过原因", "已在持仓中（跳过）：BTC LONG" in text, text)
    check("提示 force 可叠加", "<code>force</code>" in text, text)
    check("标题说明未持有", "目标仓位（未持有）" in text, text)
    check("只剩两个可复制的仓位", "2. xyz:SKHX SHORT" in text and "3. " not in text, text)


async def test_no_held_position_lists_everything():
    configure(BOARD, {})
    text = await main.handle_copy_existing()
    check("空仓时全列", "1. BTC LONG" in text and "3. xyz:SKHX SHORT" in text, text)
    check("空仓时没有跳过段", "已在持仓中" not in text and "方向相反" not in text, text)


async def test_opposite_side_is_reported_not_offered():
    configure(BOARD, {"ETH": ("LONG", 1.0, 3_100.0)})
    text = await main.handle_copy_existing()
    rows = numbered_rows(text)
    check("反向仓不进编号", "1. BTC LONG" in text and not any("ETH" in row for row in rows), text)
    check("反向仓单列一段", "方向相反（需先平仓）：ETH SHORT" in text, text)
    check("反向仓不提示 force", "要在已持有的仓位上继续加仓" not in text, text)


async def test_everything_held_says_why():
    configure(BOARD, {
        "BTC": ("LONG", 0.05, 69_000.0),
        "ETH": ("SHORT", 2.0, 3_000.0),
        "xyz:SKHX": ("SHORT", 100.0, 800.0),
    })
    text = await main.handle_copy_existing()
    check("全持有时不列表", "1. " not in text, text)
    check("全持有时报数量", "目标 3 个仓位里没有可以新开的" in text, text)
    check("全持有逐条说明", text.count("你已持有，跳过") == 3, text)
    check("全持有不下单", "不会下任何单" in text, text)


async def test_force_readmits_same_side_only():
    configure(BOARD, {
        "BTC": ("LONG", 0.05, 69_000.0),
        "ETH": ("LONG", 1.0, 3_100.0),
    })
    text = await main.handle_copy_existing(force=True)
    rows = numbered_rows(text)
    check("force 让同向已持有回到列表", "1. BTC LONG" in text, text)
    check("force 不放开反向仓", not any("ETH" in row for row in rows), text)
    check("force 后反向仓仍在跳过段", "方向相反（需先平仓）：ETH SHORT" in text, text)


async def test_pnl_arguments_still_narrow():
    configure(BOARD, {})
    text = await main.handle_copy_existing("loss")
    check("loss 只留浮亏", "1. BTC LONG" in text and "xyz:SKHX SHORT" in text, text)
    check("loss 过滤浮盈", "ETH SHORT" not in text, text)
    check("loss 在标题标注", "只看浮亏" in text, text)

    text = await main.handle_copy_existing("profit")
    check("profit 只留浮盈", "1. ETH SHORT" in text and "BTC LONG" not in text, text)


async def test_narrowed_scope_can_be_empty():
    configure([position("ETH", PositionSide.SHORT, 2.0, 3_000.0, pnl=40.0)], {})
    text = await main.handle_copy_existing("loss")
    check("收窄后为空时说明", "目标当前没有浮亏仓位" in text, text)
    check("收窄后为空时不下单", "无需操作" in text, text)


async def test_no_target_positions():
    configure([], {})
    text = await main.handle_copy_existing()
    check("目标空仓时无操作", "无需操作" in text, text)


async def test_invalid_scope_is_rejected():
    configure(BOARD, {})
    text = await main.handle_copy_existing("bogus")
    check("非法范围被拒", "无效的范围参数" in text, text)
    check("用法提示不带 loss 默认", "默认列出「目标有、你没有」的仓位" in text, text)


async def test_selection_numbers_follow_the_filtered_list():
    configure(BOARD, {"BTC": ("LONG", 0.05, 69_000.0)})
    text = await main.handle_copy_existing(None, False, [1])
    check("序号 1 对应 ETH（BTC 已被排除）", "1. ETH SHORT" in text and "✅" in text, text)
    check("选中合计只算一笔", "已选 1 笔" in text, text)

    text = await main.handle_copy_existing(None, False, [3])
    check("超出范围的序号被拒", "序号超出范围：3" in text, text)
    check("范围提示按新列表长度", "当前可选 1-2" in text, text)


async def test_policy_line_matches_new_scope():
    text = main.existing_position_policy_text()
    check("状态行说明未持有", "只列你没持有的仓位" in text, text)
    check("状态行不再提盈亏", "浮亏" not in text and "浮盈" not in text, text)


async def test_guard_still_blocks_recent_copies():
    configure(BOARD, {})
    # Point the ledger at a temp file so the run cannot leave state behind in
    # data/copyloss_ledger.json (the guard writes on every manual copy).
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["COPY_LEDGER_PATH"] = str(Path(tmp) / "ledger.json")
        before = await main.handle_copy_existing()
        check("首次没有最近复制提示", "分钟前复制过" not in before, before)
        main.settings.copy_rules.copy_duplicate_guard_minutes = 30
        main.copy_ledger.record(
            symbol="BTC", side="LONG", order_id="0xabc", notional=100.0, simulated=True
        )
        text = await main.handle_copy_existing()
        check(
            "最近复制过的标注跳过",
            "刚刚复制过，跳过" in text,
            text,
        )
    os.environ.pop("COPY_LEDGER_PATH", None)


async def test_small_copy_is_not_blocked_by_a_ten_dollar_floor():
    """A $5 proportional copy is legal on HIP-3 and must still be offered."""
    configure(BOARD, {}, balance=71.5, decimals={"BTC": 5})
    text = await main.handle_copy_existing()
    rows = numbered_rows(text)
    check("$10 以下仍进编号", any("BTC LONG" in row for row in rows), text)
    check("不再出现 $10 最低提示", "最低" not in text, text)

    # 0.35 < one lot (0.00001 BTC = $0.70): nothing the exchange could accept.
    configure(BOARD, {}, balance=5.0, decimals={"BTC": 5})
    text = await main.handle_copy_existing()
    check(
        "不足一个 tick 才标记跳过",
        "不足一个最小下单单位" in text,
        text,
    )


async def main_():
    await test_held_same_side_falls_out_of_the_list()
    await test_no_held_position_lists_everything()
    await test_opposite_side_is_reported_not_offered()
    await test_everything_held_says_why()
    await test_force_readmits_same_side_only()
    await test_pnl_arguments_still_narrow()
    await test_narrowed_scope_can_be_empty()
    await test_no_target_positions()
    await test_invalid_scope_is_rejected()
    await test_selection_numbers_follow_the_filtered_list()
    await test_policy_line_matches_new_scope()
    await test_guard_still_blocks_recent_copies()
    await test_small_copy_is_not_blocked_by_a_ten_dollar_floor()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
