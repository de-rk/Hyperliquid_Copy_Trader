"""Checks for API (agent) wallet signing.

Hyperliquid API wallets derive to a different address than the account they
trade for — that combination must initialize, and startup must refuse an
unapproved or expired agent before the first order is rejected by the
exchange.

Run:  .venv/bin/python scripts/test_agent_wallet.py
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import main  # noqa: E402
from copy_engine.executor import TradeExecutor  # noqa: E402

PASS = "✅"
FAIL = "❌"
results = []

# Throwaway material; neither the key nor these addresses touch real wallets.
AGENT_KEY = "0x" + "11" * 32
# Stand-in for the address the key derives to; the stubs inject it directly.
AGENT_ADDRESS = "0x3b17f5a296e31bdc30e9c6c66ef72f58c4d01252"
MAIN_WALLET = "0xf8bff73b4349e5cb106b216994af52094412391c"


def check(name: str, cond: bool, detail: str = ""):
    results.append((name, cond, detail))
    print(f"{PASS if cond else FAIL} {name}" + (f"  -- {detail}" if detail and not cond else ""))


class StubClient:
    def __init__(self, agents):
        self.agents = agents

    async def get_extra_agents(self, address):
        return self.agents


def build_executor(private_key=AGENT_KEY, wallet=MAIN_WALLET):
    return TradeExecutor(
        wallet_address=wallet,
        private_key=private_key,
        info_url="https://api.hyperliquid.xyz/info",
        exchange_url="https://api.hyperliquid.xyz/exchange",
        dry_run=False,
    )


def configure(agent_address, agents, wallet=MAIN_WALLET):
    main.settings.simulated_trading = False
    main.settings.hyperliquid.wallet_address = wallet
    main.client = StubClient(agents)
    main.executor = SimpleNamespace(agent_address=agent_address)


def test_executor_accepts_agent_key():
    executor = build_executor()
    check("API 私钥不再报地址不匹配", executor.account is not None)
    check("签名地址被记录", executor.agent_address == executor.account.address.lower(),
          str(executor.agent_address))
    check("交易账户仍是主钱包", executor.wallet_address == MAIN_WALLET.lower())


def test_executor_accepts_direct_key():
    executor = build_executor(wallet=None)
    # With no wallet configured, only the derived address exists.
    check("不填钱包地址时仍可初始化", executor.agent_address is not None)

    key = "0x" + "22" * 32
    from eth_account import Account
    derived = Account.from_key(key).address
    executor = build_executor(private_key=key, wallet=derived)
    check("主私钥 + 主地址照旧可用", executor.account is not None)
    check("直签时 agent 地址等于钱包地址", executor.agent_address == derived.lower())


async def test_authorization_paths():
    now_ms = int(time.time() * 1000)
    valid = {"name": "copy", "address": AGENT_ADDRESS, "validUntil": now_ms + 90 * 86_400_000}

    configure(AGENT_ADDRESS, [valid])
    await main.verify_signer_authorization()
    check("已授权的 API 钱包通过", True)

    configure(MAIN_WALLET, [])
    await main.verify_signer_authorization()
    check("直签（地址一致）跳过校验", True)

    configure(AGENT_ADDRESS, [])
    try:
        await main.verify_signer_authorization()
        check("未授权的 API 钱包被拒", False, "未抛出")
    except RuntimeError as exc:
        check("未授权的 API 钱包被拒", "not approved" in str(exc), str(exc))

    expired = dict(valid, validUntil=now_ms - 1000)
    configure(AGENT_ADDRESS, [expired])
    try:
        await main.verify_signer_authorization()
        check("过期的 API 钱包被拒", False, "未抛出")
    except RuntimeError as exc:
        check("过期的 API 钱包被拒", "expired" in str(exc), str(exc))


async def run():
    test_executor_accepts_agent_key()
    test_executor_accepts_direct_key()
    await test_authorization_paths()

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：" + "、".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
