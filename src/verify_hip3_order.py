"""HIP-3 (builder DEX) resolution and order-path verification.

Read-only by default: it resolves a builder DEX asset, prints the exchange
metadata, the follower's per-DEX state, the unified-account capacity for both
sides, and the exact order payload the bot would submit. Nothing is sent.

With ``LIVE_ORDER_TEST_CONFIRM=HIP3_12_USD`` it additionally opens at most $12
on the symbol, then reduce-only closes the exchange-reported position. It
refuses to run if the symbol already has a position.

Usage:
    python verify_hip3_order.py                 # diagnostics only
    python verify_hip3_order.py xyz:TSLA        # another builder DEX asset
    LIVE_ORDER_TEST_CONFIRM=HIP3_12_USD python verify_hip3_order.py xyz:SKHX
"""
import asyncio
import os
import sys
from decimal import Decimal, ROUND_DOWN

from config.settings import Settings
from copy_engine.executor import TradeExecutor
from hyperliquid.client import HyperliquidClient
from hyperliquid.models import OrderSide, PositionSide


DEFAULT_SYMBOL = "xyz:SKHX"
MAX_NOTIONAL_USD = Decimal("12.00")
TARGET_NOTIONAL_USD = Decimal("11.50")
MIN_NOTIONAL_USD = Decimal("10.00")
CONFIRMATION = "HIP3_12_USD"


def bounded_size(
    mid_price: float,
    size_decimals: int,
    target: Decimal,
    enforce_min: bool = True,
) -> Decimal:
    """Round down so the requested notional stays below the hard cap."""
    quantum = Decimal(1).scaleb(-size_decimals)
    size = (target / Decimal(str(mid_price))).quantize(quantum, rounding=ROUND_DOWN)
    notional = size * Decimal(str(mid_price))
    if size <= 0:
        raise RuntimeError("Requested notional rounds to zero at this tick size")
    if enforce_min and notional < MIN_NOTIONAL_USD:
        raise RuntimeError(
            f"Order is ${notional:.4f}, below Hyperliquid's $10.00 minimum; "
            f"the {size_decimals}-decimal tick size forces a larger notional"
        )
    if notional > MAX_NOTIONAL_USD:
        raise RuntimeError("Internal safety cap failed: requested order exceeds $12.00")
    return size


async def main() -> None:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SYMBOL).strip()
    settings = Settings.load()
    live = os.getenv("LIVE_ORDER_TEST_CONFIRM") == CONFIRMATION
    address = settings.hyperliquid.wallet_address

    executor = TradeExecutor(
        wallet_address=address or "0x0000000000000000000000000000000000000000",
        private_key=settings.hyperliquid.private_key if live else "",
        info_url=f"{settings.hyperliquid.api_url}/info",
        exchange_url=f"{settings.hyperliquid.api_url}/exchange",
        dry_run=not live,
        max_slippage_pct=1.0,
    )

    asset = await executor.get_asset(symbol)
    if asset is None:
        raise RuntimeError(f"{symbol} is not resolvable; check the DEX prefix and ticker")
    print(
        f"resolved {symbol}: asset={asset.asset_index} dex='{asset.dex}' "
        f"szDecimals={asset.sz_decimals} maxLeverage={asset.max_leverage}x "
        f"onlyIsolated={asset.only_isolated}"
    )

    mid_price = await executor._get_mid_price(symbol)
    size = bounded_size(mid_price, asset.sz_decimals, TARGET_NOTIONAL_USD, enforce_min=False)
    print(f"mid=${mid_price:.4f}  preview size={size} (~${size * Decimal(str(mid_price)):.2f})")

    preview = await executor.preview_order(
        symbol=symbol, side=OrderSide.BUY, size=size
    )
    print(f"order payload would be: {preview}")

    async with HyperliquidClient(settings.hyperliquid.api_url) as client:
        universe = await client.get_all_assets()
        same_dex = [item for item in universe if item["dex"] == asset.dex]
        print(
            f"universe: {len(universe)} assets across "
            f"{len({item['dex'] for item in universe})} DEXs; '{asset.dex}' has {len(same_dex)}"
        )
        price = await client.get_market_price(symbol)
        print(f"client.get_market_price({symbol}) = {price}")

        if not address:
            print("no wallet configured; stopping after symbol-level checks")
            return

        dex_state = await client.get_user_state(address, dex=asset.dex)
        if dex_state is None:
            raise RuntimeError(f"cannot read the follower's '{asset.dex}' DEX state")
        print(
            f"follower '{asset.dex or 'default'}' DEX: equity=${dex_state.balance:,.2f} "
            f"marginUsed=${dex_state.margin_used:,.2f} positions={len(dex_state.positions)}"
        )
        for position in dex_state.positions:
            print(
                f"  {position.symbol} {position.side.value} size={position.size} "
                f"value=${position.notional_value:,.2f} lev={position.leverage}x"
            )

        for label, side in (("buy", OrderSide.BUY), ("sell", OrderSide.SELL)):
            capacity = await client.get_available_to_trade_margin(
                address, symbol, side.value
            )
            if capacity is None:
                print(
                    f"availableToTrade({label}) = None — activeAssetData rejected "
                    "the symbol (check exact casing)"
                )
            else:
                print(
                    f"availableToTrade({label}) = ${capacity:,.2f} collateral "
                    "(multiply by leverage for the notional it can back)"
                )

    if not live:
        print("dry diagnostics complete; set LIVE_ORDER_TEST_CONFIRM=HIP3_12_USD to place a $12 order")
        return

    if settings.simulated_trading:
        raise RuntimeError("Refusing live verification: SIMULATED_TRADING must be false")
    if not address or not settings.hyperliquid.private_key:
        raise RuntimeError("Refusing live verification: wallet credentials are missing")

    async with HyperliquidClient(settings.hyperliquid.api_url) as client:
        before = await client.get_user_state(address, dex=asset.dex)
        if before is None:
            raise RuntimeError("Refusing verification: cannot read the follower DEX state")
        if any(position.symbol == symbol for position in before.positions):
            raise RuntimeError(f"Refusing verification: wallet already holds {symbol}")

        if not await executor._update_leverage(symbol, 1):
            raise RuntimeError("Refusing verification: cannot set 1x leverage")

        size = bounded_size(mid_price, asset.sz_decimals, TARGET_NOTIONAL_USD)
        print(f"opening {symbol} size={size} notional~${size * Decimal(str(mid_price)):.2f}")
        open_order_id = await executor.execute_market_order(
            symbol=symbol, side=OrderSide.BUY, size=size, leverage=1
        )
        if not open_order_id:
            raise RuntimeError(f"{symbol} opening order was rejected: {executor.last_error}")
        print(f"opening accepted: order_id={open_order_id}")

        await asyncio.sleep(1)
        after_open = await client.get_user_state(address, dex=asset.dex)
        position = next(
            (item for item in (after_open.positions if after_open else []) if item.symbol == symbol),
            None,
        )
        if position is None or position.size <= 0:
            raise RuntimeError("Opening accepted but no position is reported; inspect fills")
        if position.notional_value > float(MAX_NOTIONAL_USD) * 1.01:
            raise RuntimeError("Refusing close: reported position exceeds the test cap")
        print(
            f"position confirmed: {position.side.value} size={position.size} "
            f"value=${position.notional_value:.2f}"
        )

        close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        close_order_id = await executor.close_position(
            symbol=symbol, size=Decimal(str(position.size)), side=close_side
        )
        if not close_order_id:
            raise RuntimeError(f"reduce-only close rejected: {executor.last_error}")
        print(f"reduce-only close accepted: order_id={close_order_id}")

        await asyncio.sleep(1)
        after_close = await client.get_user_state(address, dex=asset.dex)
        remaining = next(
            (item for item in (after_close.positions if after_close else []) if item.symbol == symbol),
            None,
        )
        if remaining is not None and remaining.size > 0:
            raise RuntimeError(f"Close accepted but position remains: size={remaining.size}")
        print(f"{symbol} verification PASSED: HIP-3 order path accepted, position flat")


if __name__ == "__main__":
    asyncio.run(main())
