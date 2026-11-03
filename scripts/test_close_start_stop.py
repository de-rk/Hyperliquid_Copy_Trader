"""Standalone checks for /start, /stop and /close (no network, no orders).

Run:  .venv/bin/python scripts/test_close_start_stop.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402  (imports settings; simulated by default without .env)
from hyperliquid.models import OrderSide  # noqa: E402

PASS = "✅"
FAIL = "❌"
results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


class StubClient:
    async def get_market_price(self, symbol):
        return {"BTC": 104000.0, "xyz:SKHX": 1000.0, "ETH": 3800.0}.get(symbol, 0.0)


class StubExecutor:
    def __init__(self, fail_for=None):
        self.fail_for = fail_for or set()
        self.calls = []
        self.last_error = ""

    async def cancel_all_orders(self, symbol=None):
        return 0

    async def close_position(self, symbol, size=None, side=None):
        if symbol in self.fail_for:
            self.last_error = "insufficient margin (stub)"
            return None
        self.calls.append((symbol, float(size), side))
        return f"oid-{symbol}"


class StubMonitor:
    async def stop_monitoring(self):
        return None


async def seed_sim_book():
    main.simulated_positions.clear()
    main.simulated_positions["BTC"] = {
        "size": 0.5, "entry_price": 98000.0, "side": "LONG", "leverage": 5,
        "value": 0.5 * 98000.0,
    }
    main.simulated_positions["xyz:SKHX"] = {
        "size": -300.0, "entry_price": 1100.0, "side": "SHORT", "leverage": 5,
        "value": 300 * 1100.0,
    }
    main.simulated_positions["ETH"] = {
        "size": 2.0, "entry_price": 4000.0, "side": "LONG", "leverage": 3,
        "value": 2.0 * 4000.0,
    }
    main.simulated_balance = 10000.0
    main.simulated_pnl = 0.0


async def test_preview_all():
    await seed_sim_book()
    main.client = StubClient()
    main.executor = StubExecutor()
    text = await main.handle_close(None, False)
    check("预览包含全部 3 笔", text.count("\n") >= 3 and "1. BTC LONG" in text, text)
    check("预览标注模拟", "模拟" in text)
    check("预览不发单", not main.executor.calls, str(main.executor.calls))
    check("预览有 confirm 提示", "/close all confirm" in text, text)


async def test_preview_by_symbol_and_number():
    await seed_sim_book()
    main.client = StubClient()
    main.executor = StubExecutor()
    text = await main.handle_close(["skhx"], False)
    check("币种片段选中 xyz:SKHX", "已选 1 笔" in text and "xyz:SKHX" in text, text)
    check("币种选择提示正确序号", "/close 2 confirm" in text, text)

    text = await main.handle_close(["1", "3"], False)
    check("序号选中 BTC+ETH", "已选 2 笔" in text, text)

    text = await main.handle_close(["9"], False)
    check("越界序号报错", "无法识别的序号或币种" in text and "9" in text, text)

    text = await main.handle_close(["DOGE"], False)
    check("未知币种报错", "DOGE" in text, text)


async def test_execute_subset():
    await seed_sim_book()
    main.client = StubClient()
    main.executor = StubExecutor()
    text = await main.handle_close(["BTC"], True)
    check("执行后回执成功", "1. ✅ <b>BTC SELL</b>" in text, text)
    check("回执含订单号", "oid-BTC" in text, text)
    check("只发一笔", len(main.executor.calls) == 1, str(main.executor.calls))
    symbol, size, side = main.executor.calls[0]
    check("reduce-only 尺寸正确", symbol == "BTC" and size == 0.5, str(main.executor.calls[0]))
    check("方向为反手 SELL", side == OrderSide.SELL, str(side))
    check("模拟账本已删 BTC", "BTC" not in main.simulated_positions, str(main.simulated_positions.keys()))
    check("其余仓位保留", "xyz:SKHX" in main.simulated_positions and "ETH" in main.simulated_positions)
    check("模拟盈亏已入账", main.simulated_pnl == 0.5 * (104000.0 - 98000.0), str(main.simulated_pnl))


async def test_execute_all_and_short():
    await seed_sim_book()
    main.client = StubClient()
    main.executor = StubExecutor()
    text = await main.handle_close(["all"], True)
    check("all 平掉 3 笔", len(main.executor.calls) == 3, str(main.executor.calls))
    skhx_call = next(c for c in main.executor.calls if c[0] == "xyz:SKHX")
    check("空头用 BUY 平", skhx_call[2] == OrderSide.BUY, str(skhx_call))
    check("模拟账本清空", not main.simulated_positions, str(main.simulated_positions))


async def test_failure_receipt():
    await seed_sim_book()
    main.client = StubClient()
    main.executor = StubExecutor(fail_for={"BTC"})
    text = await main.handle_close(["BTC", "ETH"], True)
    check("失败项出现在回执", "❌ <b>BTC" in text and "insufficient margin" in text, text)
    check("成功项也出现", "✅ <b>ETH" in text, text)
    check("失败仓位保留", "BTC" in main.simulated_positions, str(main.simulated_positions.keys()))
    check("成功仓位删除", "ETH" not in main.simulated_positions)


async def test_empty_book():
    main.simulated_positions.clear()
    main.client = StubClient()
    main.executor = StubExecutor()
    text = await main.handle_close(None, False)
    check("空仓提示", "没有任何持仓" in text, text)


async def test_start_stop_cycle():
    main.client = StubClient()
    main.executor = StubExecutor()
    main.is_stopped = False
    main.is_paused = False
    main.paused_since = None
    main.monitor = StubMonitor()

    text = await main.handle_start()
    check("已运行时 /start 提示", "本来就在运行" in text, text)

    # simulate /stop
    await main.handle_stop()
    check("停止后 is_stopped", main.is_stopped and main.is_paused)
    check("停止保留仓位", "BTC" in main.simulated_positions if main.simulated_positions else True)

    # /close works while stopped
    await seed_sim_book()
    text = await main.handle_close(["BTC"], True)
    check("停止状态仍可平仓", "✅ <b>BTC SELL</b>" in text, text)

    # /start after stop wakes the monitor loop
    old_monitor = main.monitor
    text = await main.handle_start()
    check("停止后 /start 恢复", "已恢复跟单" in text, text)
    check("标志位清零", not main.is_stopped and not main.is_paused)
    check("监控已换新", main.monitor is not old_monitor)
    check("restart_requested 已置位", main.restart_requested.is_set())
    main.restart_requested.clear()

    # /start while merely paused
    main.is_paused = True
    main.paused_since = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    text = await main.handle_start()
    check("暂停后 /start 恢复复制", "已恢复复制" in text and not main.is_paused, text)


async def main_async():
    await test_preview_all()
    await test_preview_by_symbol_and_number()
    await test_execute_subset()
    await test_execute_all_and_short()
    await test_failure_receipt()
    await test_empty_book()
    await test_start_stop_cycle()

    failed = [name for name, ok, _ in results if not ok]
    print()
    print(f"共 {len(results)} 项，失败 {len(failed)} 项")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main_async())
