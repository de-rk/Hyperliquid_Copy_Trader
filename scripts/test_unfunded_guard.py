"""Standalone checks for the "live wallet has $0" guard.

When a live start finds $0 equity the bot must keep running, stop copying and
refuse only /orders, /copyloss and /close; /start must re-check the balance and
resume once money arrives.

Run:  .venv/bin/python scripts/test_unfunded_guard.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from telegram_bot.bot import TelegramBot  # noqa: E402

PASS = "✅"
FAIL = "❌"
results = []


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


class StubClient:
    """Follower equity controllable per call; target equity fixed."""

    TARGET = "0xtarget0000000000000000000000000000000000"
    FOLLOWER = "0xfollower00000000000000000000000000000000"

    def __init__(self, follower_values):
        self.follower_values = list(follower_values)
        self.last_follower = follower_values[-1] if follower_values else None

    async def get_portfolio_account_value(self, address):
        if address == self.TARGET:
            return 600_000.0
        if not self.follower_values:
            return self.last_follower
        self.last_follower = self.follower_values.pop(0)
        return self.last_follower


class StubExecutor:
    async def cancel_all_orders(self, symbol=None):
        return 0


class FakeMessage:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        self.texts.append(text)
        return None


class FakeUpdate:
    def __init__(self, chat_id="1"):
        self.effective_chat = type("Chat", (), {"id": int(chat_id)})()
        self.message = FakeMessage()


def make_bot() -> TelegramBot:
    bot = TelegramBot.__new__(TelegramBot)
    bot.allowed_chat_id = "1"
    bot.blocked_commands_callback = main.blocked_command_text
    bot.get_orders_callback = None
    bot.on_copy_existing_requested = None
    bot.on_close_positions_requested = None
    bot.get_status_callback = None
    return bot


async def test_block_text():
    main.wallet_unfunded = False
    check("有资金时不拦截", main.unfunded_block_text() is None)

    main.wallet_unfunded = True
    text = main.unfunded_block_text()
    check("无资金时有拦截文案", bool(text))
    check("文案点名三个命令", all(cmd in text for cmd in ("/orders", "/copyloss", "/close")), text)
    check("文案说明恢复方式", "/start" in text, text)


async def test_handlers_blocked():
    main.wallet_unfunded = True
    main.client = StubClient([0.0])
    main.executor = StubExecutor()

    text = await main.handle_copy_existing("loss")
    check("copyloss 被拦", "没有资金" in text, text)

    text = await main.handle_close(None, False)
    check("close 被拦", "没有资金" in text, text)

    bot = make_bot()
    update = FakeUpdate()
    ctx = type("Ctx", (), {"args": []})()
    await bot._orders_command(update, ctx)
    check("orders 被拦", update.message.texts and "没有资金" in update.message.texts[-1],
          str(update.message.texts))

    update = FakeUpdate()
    await bot._close_command(update, ctx)
    check("bot close 被拦", update.message.texts and "没有资金" in update.message.texts[-1],
          str(update.message.texts))

    update = FakeUpdate()
    await bot._copyloss_command(update, ctx)
    check("bot copyloss 被拦", update.message.texts and "没有资金" in update.message.texts[-1],
          str(update.message.texts))


async def test_other_commands_allowed():
    main.wallet_unfunded = True
    bot = make_bot()
    check("拦截回调对只读命令返回 None 以外的东西只用于三个命令",
          await bot._blocked_reason() is not None)
    # /status must still answer: the callback is wired independently.
    called = {}

    async def status():
        called["status"] = True
        return "📊 状态"

    bot.get_status_callback = status
    update = FakeUpdate()
    await bot._status_command(update, type("Ctx", (), {"args": []})())
    check("status 在无资金时仍可用", called.get("status") and update.message.texts[-1] == "📊 状态",
          str(update.message.texts))


async def test_start_blocks_when_still_empty():
    main.wallet_unfunded = True
    main.is_paused = True
    main.is_stopped = False
    main.client = StubClient([0.0, 0.0])
    main.executor = StubExecutor()

    text = await main.handle_start()
    check("start 仍为 0 时拒绝恢复", "$0.00" in text, text)
    check("start 仍为 0 时保持暂停", main.is_paused is True)
    check("start 仍为 0 时标记未清除", main.wallet_unfunded is True)


async def test_start_resumes_when_funded():
    main.wallet_unfunded = True
    main.is_paused = True
    main.is_stopped = False
    main.client = StubClient([250.0])  # follower now funded
    main.executor = StubExecutor()

    text = await main.handle_start()
    check("入金后 start 恢复", "已恢复跟单" in text, text)
    check("入金后清除未资金标记", main.wallet_unfunded is False)
    check("入金后解除暂停", main.is_paused is False)
    check("余额已更新", abs(main.simulated_balance - 250.0) < 1e-6, str(main.simulated_balance))
    check(
        "比例按新余额重算",
        abs(main.settings.sizing.portfolio_ratio - 250.0 / 600_000.0) < 1e-9,
        str(main.settings.sizing.portfolio_ratio),
    )


async def test_funded_passthrough():
    main.wallet_unfunded = False
    main.is_paused = False
    main.is_stopped = False
    main.client = StubClient([0.0])
    main.executor = StubExecutor()
    text = await main.handle_start()
    check("正常状态 start 无需动作", "本来就在运行中" in text, text)


async def main_():
    main.settings.target_wallet = StubClient.TARGET
    main.settings.hyperliquid.wallet_address = StubClient.FOLLOWER
    main.settings.simulated_trading = False  # the guard only exists in live mode

    await test_block_text()
    await test_handlers_blocked()
    await test_other_commands_allowed()
    await test_start_blocks_when_still_empty()
    await test_start_resumes_when_funded()
    await test_funded_passthrough()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_()))
