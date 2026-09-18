"""One-time, bounded verification of the mainnet ETH order path.

The script is separate from copy trading. It needs an explicit confirmation,
opens at most $12 of ETH on the default Perp DEX, then reduce-only closes the
exchange-reported position. It refuses to run if an ETH position already exists.
"""
import asyncio
import os
from decimal import Decimal, ROUND_DOWN

from config.settings import Settings
from copy_engine.executor import TradeExecutor
from hyperliquid.client import HyperliquidClient
from hyperliquid.models import OrderSide, PositionSide


SYMBOL = "ETH"
MAX_NOTIONAL_USD = Decimal("12.00")
TARGET_NOTIONAL_USD = Decimal("11.50")
MIN_NOTIONAL_USD = Decimal("10.00")
CONFIRMATION = "ETH_12_USD"


def bounded_size(mid_price: float, size_decimals: int) -> Decimal:
    """Round down so the requested ETH notional remains below the hard cap."""
    quantum = Decimal(1).scaleb(-size_decimals)
    size = (TARGET_NOTIONAL_USD / Decimal(str(mid_price))).quantize(
        quantum, rounding=ROUND_DOWN
    )
    notional = size * Decimal(str(mid_price))
    if size <= 0 or notional < MIN_NOTIONAL_USD:
        raise RuntimeError(
            f"ETH order is ${notional:.4f}, below Hyperliquid's $10.00 minimum"
        )
    if notional > MAX_NOTIONAL_USD:
        raise RuntimeError("Internal safety cap failed: requested order exceeds $12.00")
    return size


async def main() -> None:
    settings = Settings.load()
    if settings.simulated_trading:
        raise RuntimeError("Refusing verification: SIMULATED_TRADING must be false")
    if os.getenv("LIVE_ORDER_TEST_CONFIRM") != CONFIRMATION:
        raise RuntimeError(
            "Refusing verification: set LIVE_ORDER_TEST_CONFIRM=ETH_12_USD"
        )
    if not settings.hyperliquid.wallet_address or not settings.hyperliquid.private_key:
        raise RuntimeError("Refusing verification: live wallet credentials are missing")

    executor = TradeExecutor(
        wallet_address=settings.hyperliquid.wallet_address,
        private_key=settings.hyperliquid.private_key,
        info_url=f"{settings.hyperliquid.api_url}/info",
        exchange_url=f"{settings.hyperliquid.api_url}/exchange",
        dry_run=False,
        # The requested notional is $11.50, so a 1% IOC price allowance
        # remains below the $12.00 hard test cap.
        max_slippage_pct=1.0,
    )

    async with HyperliquidClient(settings.hyperliquid.api_url) as client:
        before = await client.get_user_state(
            settings.hyperliquid.wallet_address, dex=""
        )
        if before is None:
            raise RuntimeError("Refusing verification: cannot read default Perp state")
        if any(position.symbol == SYMBOL for position in before.positions):
            raise RuntimeError("Refusing verification: wallet already has an ETH position")

        if not await executor._update_leverage(SYMBOL, 1):
            raise RuntimeError("Refusing verification: cannot set ETH leverage to 1x")

        asset_index, size_decimals = await executor._get_asset_info(SYMBOL)
        mid_price = await executor._get_mid_price(SYMBOL)
        size = bounded_size(mid_price, size_decimals)
        requested_notional = size * Decimal(str(mid_price))
        print(
            f"ETH verification opening: asset={asset_index}, size={size}, "
            f"mid=${mid_price:.4f}, requested_notional=${requested_notional:.2f}"
        )

        open_order_id = await executor.execute_market_order(
            symbol=SYMBOL,
            side=OrderSide.BUY,
            size=size,
            leverage=1,
        )
        if not open_order_id:
            raise RuntimeError("ETH opening order was rejected")
        print(f"ETH opening accepted: order_id={open_order_id}")

        await asyncio.sleep(1)
        after_open = await client.get_user_state(
            settings.hyperliquid.wallet_address, dex=""
        )
        if after_open is None:
            raise RuntimeError("Opening accepted but default Perp state cannot be read")
        position = next(
            (item for item in after_open.positions if item.symbol == SYMBOL), None
        )
        if position is None or position.size <= 0:
            raise RuntimeError("Opening accepted but no ETH position is reported; inspect fills")
        if position.notional_value > float(MAX_NOTIONAL_USD) * 1.01:
            raise RuntimeError("Refusing close: exchange-reported ETH position exceeds test cap")

        print(
            f"ETH position confirmed: side={position.side.value}, "
            f"size={position.size}, value=${position.notional_value:.2f}"
        )
        close_side = (
            OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        )
        close_order_id = await executor.close_position(
            symbol=SYMBOL,
            size=Decimal(str(position.size)),
            side=close_side,
        )
        if not close_order_id:
            raise RuntimeError("ETH reduce-only close was rejected; ETH may remain open")
        print(f"ETH reduce-only close accepted: order_id={close_order_id}")

        await asyncio.sleep(1)
        after_close = await client.get_user_state(
            settings.hyperliquid.wallet_address, dex=""
        )
        if after_close is None:
            raise RuntimeError("Close accepted but default Perp state cannot be read")
        remaining = next(
            (item for item in after_close.positions if item.symbol == SYMBOL), None
        )
        if remaining is not None and remaining.size > 0:
            raise RuntimeError(
                f"Reduce-only close accepted but ETH remains: size={remaining.size}"
            )
        print("ETH verification PASSED: order path accepted and ETH position is flat")


if __name__ == "__main__":
    asyncio.run(main())
