import asyncio
import html
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo
from loguru import logger
from config.settings import settings
from utils.logger import setup_logger
from hyperliquid.client import HyperliquidClient
from hyperliquid.websocket import HyperliquidWebSocket
from hyperliquid.models import WebSocketUpdate, PositionSide, OrderSide
from copy_engine import WalletMonitor, TradeExecutor, PositionSizer

# Setup logging
setup_logger(settings.log_file, settings.log_level)

# Hyperliquid minimum order size requirement
MIN_POSITION_SIZE_USD = 10.0

# Initialize components
monitor: WalletMonitor = None
executor: TradeExecutor = None
position_sizer: PositionSizer = None
client: HyperliquidClient = None
telegram_bot: Any = None
notifier: Any = None

# State tracking
is_paused = False
trades_copied_count = 0
bot_start_time = None

# Simulated account tracking
simulated_balance = 0.0
simulated_positions = {}  # symbol -> {'size': float, 'entry_price': float, 'side': str}
simulated_pnl = 0.0
processed_fill_ids: set[str] = set()
MAX_PROCESSED_FILL_IDS = 10_000
# target oid -> follower order metadata. Keeping this mapping in memory is
# sufficient because the target websocket is the source of truth for this run;
# a restart takes a fresh baseline and never cancels unrelated follower orders.
mirrored_orders: dict[str, dict] = {}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def perp_dex_for_symbol(symbol: str) -> str:
    """Return the DEX that owns a perp symbol's collateral and positions."""
    return symbol.split(":", 1)[0] if ":" in symbol else ""


def calculate_proportional_close_size(
    target_fill_size: float,
    target_remaining_size: float,
    follower_size: float,
) -> tuple[float, float]:
    """Return a follower close size from the target's pre-close position ratio.

    ``target_remaining_size`` is the target position after this fill. A full
    close therefore has a remaining size of zero and closes the whole follower
    position. The returned size is always capped to the actual follower size.
    """
    if target_fill_size <= 0 or follower_size <= 0:
        return 0.0, 0.0

    target_pre_close_size = max(0.0, target_remaining_size) + target_fill_size
    if target_pre_close_size <= 0:
        return 0.0, 0.0

    close_ratio = min(1.0, target_fill_size / target_pre_close_size)
    return min(follower_size, follower_size * close_ratio), close_ratio


async def get_follower_balance() -> float | None:
    """Return the balance used for live sizing, or the simulated balance."""
    if settings.simulated_trading:
        return simulated_balance
    if not settings.hyperliquid.wallet_address:
        logger.error("Live sizing requires HYPERLIQUID_WALLET_ADDRESS")
        return None
    follower_state = await client.get_user_state(settings.hyperliquid.wallet_address)
    if follower_state is None:
        logger.error("Unable to read follower wallet state")
        return None
    return follower_state.balance


def calculate_adjusted_leverage(target_leverage: float, adjustment_ratio: float, symbol: str) -> int:
    """
    Calculate adjusted leverage with proper rounding and max leverage limits.
    
    Hyperliquid only supports integer leverage (1x, 2x, 3x, etc.)
    Each asset has different max leverage limits.
    
    Args:
        target_leverage: Target wallet's leverage
        adjustment_ratio: Multiplier (e.g., 0.5 = use 50% of target's leverage)
        symbol: Trading symbol (for max leverage lookup)
    
    Returns:
        Integer leverage between 1 and the asset's max leverage
    """
    # Asset-specific max leverage limits on Hyperliquid
    MAX_LEVERAGE_LIMITS = {
        'BTC': 50,
        'ETH': 50,
        'SOL': 20,
        'MATIC': 20,
        'ARB': 20,
        'OP': 20,
        'AVAX': 20,
        'DOGE': 20,
        'ATOM': 10,
        'LTC': 10,
        'BCH': 10,
        'LINK': 10,
        'UNI': 10,
        'APE': 10,
        'APT': 10,
        'SUI': 10,
        'TIA': 10,
        'SEI': 10,
        'WLD': 10,
        'NEAR': 10,
        'FET': 10,
        'INJ': 10,
        'STX': 10,
        'PEPE': 10,
        'BONK': 10,
        'WIF': 10,
        'HYPE': 10,
        'ZEC': 10,
        'TRUMP': 10,
        'MELANIA': 10,
        'PUMP': 10,
    }
    
    # Get max leverage for this asset (default to 10x if unknown)
    max_leverage = MAX_LEVERAGE_LIMITS.get(symbol.upper(), 10)
    
    # Calculate desired leverage
    desired_leverage = target_leverage * adjustment_ratio
    
    # Round to nearest integer
    rounded_leverage = round(desired_leverage)
    
    # Ensure minimum of 1x
    rounded_leverage = max(1, rounded_leverage)
    
    # Cap at asset's max leverage
    final_leverage = min(rounded_leverage, max_leverage)
    
    return final_leverage


async def on_new_position(position_data: dict):
    """
    Called when target wallet opens a new position
    This is where we copy the trade!
    """
    global trades_copied_count, is_paused, simulated_balance, simulated_positions, simulated_pnl
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping trade")
        return
    
    # Check max open trades limit
    if settings.copy_rules.max_open_trades is not None:
        current_trades = len(monitor.current_state.positions) if monitor.current_state else 0
        if current_trades >= settings.copy_rules.max_open_trades:
            logger.warning(f"⚠️ Max open trades limit reached ({current_trades}/{settings.copy_rules.max_open_trades}) - skipping trade")
            return
    
    # Check account equity limit
    if settings.copy_rules.max_account_equity is not None:
        current_equity = monitor.current_state.total_equity if monitor.current_state else 0
        if current_equity >= settings.copy_rules.max_account_equity:
            logger.warning(f"⚠️ Max account equity reached (${current_equity:,.2f}/${settings.copy_rules.max_account_equity:,.2f}) - stopping copy trading")
            is_paused = True
            if notifier:
                await notifier.send_error_notification(f"Max account equity reached: ${current_equity:,.2f}. Bot paused automatically.")
            return
    
    try:
        logger.success("=" * 60)
        logger.success("🎯 NEW POSITION DETECTED - COPYING TRADE!")
        logger.success("=" * 60)
        
        # Parse position data
        symbol = position_data.get("coin", "")
        size = float(position_data.get("szi", 0))
        side = PositionSide.LONG if size > 0 else PositionSide.SHORT
        
        # REST snapshots wrap this under ``position`` while websocket
        # position updates can provide the fields at the top level.
        position_info = position_data.get("position", position_data)
        entry_price = float(position_info.get("entryPx", 0))
        target_leverage = float(position_info.get("leverage", {}).get("value", 1))
        
        logger.info(f"📊 Target Position:")
        logger.info(f"   Symbol: {symbol}")
        logger.info(f"   Side: {side.value.upper()}")
        logger.info(f"   Size: {abs(size)}")
        logger.info(f"   Entry: ${entry_price:,.2f}")
        logger.info(f"   Leverage: {target_leverage}x")
        
        # Get current market price
        async with client:
            current_price = await client.get_market_price(symbol)
            if not current_price:
                current_price = entry_price
        
        logger.info(f"   Current Price: ${current_price:,.2f}")
        
        # Check if we should copy this position (entry quality)
        should_copy = position_sizer.should_copy_position(
            entry_price,
            current_price,
            settings.copy_rules.min_entry_quality_pct
        )
        
        if not should_copy:
            logger.warning("⚠️ Skipping - Entry quality check failed")
            return
        
        # Get target wallet balance
        target_state = monitor.current_state
        target_balance = target_state.balance if target_state else 100000  # Default if unknown
        
        # Calculate your position size from the follower's actual balance.
        your_balance = await get_follower_balance()
        if your_balance is None:
            return
        your_exposure = 0  # TODO: Calculate current exposure
        
        # Simplified calculation for now
        if settings.sizing.mode == "proportional":
            ratio = your_balance / target_balance if target_balance > 0 else settings.sizing.portfolio_ratio
            your_size = abs(size) * ratio
        else:
            your_size = settings.sizing.fixed_size / entry_price if entry_price > 0 else 0
        
        # Calculate adjusted leverage
        your_leverage = position_sizer.calculate_leverage(
            target_leverage,
            settings.leverage.adjustment_ratio,
            settings.leverage.max_leverage,
            settings.leverage.min_leverage
        )
        
        # Check minimum position size (Hyperliquid requirement)
        your_position_value = your_size * entry_price
        if your_position_value < MIN_POSITION_SIZE_USD:
            logger.warning("")
            logger.warning(f"⚠️  Skipping Position: {symbol}")
            logger.warning(f"   Position value ${your_position_value:.2f} below Hyperliquid minimum ${MIN_POSITION_SIZE_USD:.2f}")
            logger.success("=" * 60)
            return
        
        logger.info("")
        logger.info(f"Your Position:")
        logger.info(f"   Size: {your_size:.4f} {symbol}")
        logger.info(f"   Notional: ${your_size * entry_price:,.2f}")
        logger.info(f"   Leverage: {your_leverage}x")
        logger.info(f"   Entry: ${entry_price:,.2f}")
        
        # Execute the trade
        logger.info("")
        logger.info(f"Executing trade...")
        result = await executor.execute_market_order(
            symbol=symbol,
            side=OrderSide.BUY if side == PositionSide.LONG else OrderSide.SELL,
            size=your_size,
            leverage=your_leverage
        )
        
        if result:
            logger.success(f"✅ Trade executed successfully!")
            logger.success(f"   Result: {result}")
            trades_copied_count += 1
            
            # Send Telegram notification
            if notifier:
                await notifier.send_trade_notification(
                    symbol=symbol,
                    side=side.value,
                    size=your_size,
                    entry_price=entry_price,
                    leverage=your_leverage,
                    target_size=abs(size),
                    is_simulated=executor.dry_run
                )
        else:
            logger.error("❌ Trade execution failed")
            if notifier:
                await _notify_copy_failure(
                    symbol=symbol,
                    direction=side.value,
                    target_size=abs(size),
                    follower_size=your_size,
                    price=entry_price,
                    category="交易所拒绝或执行器失败",
                    reason=getattr(executor, "last_error", None) or "Executor returned no order id",
                )
        
        logger.success("=" * 60)
        
    except Exception as e:
        logger.error(f"Error copying position: {e}")
        if notifier:
            await _notify_copy_failure(
                symbol=str(position_data.get("coin", "unknown")),
                direction="position",
                target_size=abs(float(position_data.get("szi", 0) or 0)),
                category="程序异常",
                reason=str(e),
            )


async def on_position_close(position_data: dict):
    """Called when target wallet closes a position"""
    global simulated_balance, simulated_positions, simulated_pnl
    
    symbol = position_data.get("coin", "")
    logger.info(f"🔴 Target closed position: {symbol}")
    
    # Close simulated position and calculate PnL
    if settings.simulated_trading and symbol in simulated_positions:
        pos = simulated_positions[symbol]
        # Get current price from monitor
        current_price = 0
        if monitor.current_state:
            for p in monitor.current_state.positions:
                if p.symbol == symbol:
                    current_price = p.current_price
                    break
        
        if current_price > 0:
            # Calculate PnL
            if pos['side'] == 'LONG':
                pnl = pos['size'] * (current_price - pos['entry_price'])
            else:
                pnl = abs(pos['size']) * (pos['entry_price'] - current_price)
            
            # Return margin to balance
            margin_used = pos['value'] / pos['leverage']
            simulated_balance += margin_used + pnl
            simulated_pnl += pnl
            
            logger.success("")
            logger.success(f"💰 SIMULATED POSITION CLOSED!")
            logger.success(f"   Entry: ${pos['entry_price']:,.2f}")
            logger.success(f"   Exit: ${current_price:,.2f}")
            logger.success(f"   PnL: ${pnl:,.2f} ({(pnl/pos['value']*100):+.2f}%)")
            logger.success(f"   New Balance: ${simulated_balance:,.2f}")
            logger.success(f"   Total PnL: ${simulated_pnl:,.2f}")
            
            del simulated_positions[symbol]
    
    # Close the corresponding follower position. A reduce-only order must use
    # the follower's actual size and the opposite side; a placeholder size can
    # leave the position open or create a new position.
    logger.info("   -> Closing your position...")
    if settings.simulated_trading:
        await executor.close_position(symbol)
    else:
        follower_state = await client.get_user_state(settings.hyperliquid.wallet_address)
        follower_position = next(
            (position for position in (follower_state.positions if follower_state else [])
             if position.symbol == symbol),
            None,
        )
        if follower_position is None:
            logger.info(f"No follower position found for {symbol}; nothing to close")
            return
        close_side = OrderSide.SELL if follower_position.side == PositionSide.LONG else OrderSide.BUY
        await executor.close_position(
            symbol,
            size=Decimal(str(follower_position.size)),
            side=close_side,
        )


async def on_position_update(position_data: dict):
    """Called when target wallet updates a position"""
    symbol = position_data.get("coin", "")
    size = float(position_data.get("szi", 0))
    logger.info(f"📊 Target updated position: {symbol} (new size: {size})")
    
    # TODO: Update your position to match


async def on_new_order(order_data: dict):
    """Mirror a target resting order immediately and remember its oid mapping."""
    global mirrored_orders
    try:
        if not settings.copy_rules.mirror_pending_orders:
            return
        if is_paused:
            logger.warning("Bot is paused - skipping pending order mirror")
            return

        symbol = order_data.get('coin', '')
        side = order_data.get('side', '')
        target_size = abs(float(order_data.get('sz', order_data.get('origSz', 0))))
        price = float(order_data.get('limitPx', order_data.get('px', 0)) or 0)
        target_oid = str(order_data.get('_target_oid', order_data.get('oid', '')) or '')
        side_code = side.value if isinstance(side, OrderSide) else str(side).lower()
        is_buy = side_code in ('b', 'buy')
        position_side = PositionSide.LONG if is_buy else PositionSide.SHORT
        reduce_only = bool(order_data.get("reduceOnly", order_data.get("reduce_only", False)))

        if target_oid and target_oid in mirrored_orders:
            return
        if not symbol or not target_oid or target_size <= 0 or price <= 0:
            await _notify_copy_failure(
                symbol=symbol or "unknown", direction=position_side.value,
                target_size=target_size, follower_size=0, price=price,
                category="挂单数据无效", reason=f"Missing oid/symbol/size/price: {order_data}",
                fill_id=target_oid,
                stage="镜像交易",
            )
            return
        if not settings.copy_rules.mirror_order_price:
            try:
                price = await executor._get_mid_price(symbol)
            except Exception as exc:
                await _notify_copy_failure(
                    symbol=symbol, direction=position_side.value, target_size=target_size,
                    follower_size=0, price=price, category="镜像价格查询失败",
                    reason=str(exc), fill_id=target_oid,
                    stage="镜像交易",
                )
                return
        if settings.copy_rules.max_open_orders is not None:
            active_mirrors = sum(1 for item in mirrored_orders.values() if item.get("active", True))
            if active_mirrors >= settings.copy_rules.max_open_orders:
                await _notify_copy_failure(
                    symbol=symbol, direction=position_side.value, target_size=target_size,
                    follower_size=0, price=price, category="达到最大挂单数",
                    reason=f"Mirrored open orders {active_mirrors} >= limit {settings.copy_rules.max_open_orders}",
                    fill_id=target_oid,
                    stage="镜像交易",
                )
                return

        target_position = _target_position(symbol)
        target_leverage = target_position.leverage if target_position else 1.0
        # Match fill/error notifications: describe the position action, not
        # the raw execution side (BUY/SELL).
        notification_side = (
            f"CLOSE {target_position.side.value.upper()}"
            if reduce_only and target_position
            else f"OPEN {position_side.value.upper()}"
        )
        if settings.copy_rules.auto_adjust_size:
            follower_balance = await get_follower_balance()
            target_balance = monitor.current_state.balance if monitor.current_state else 0
            ratio = (
                follower_balance / target_balance
                if follower_balance is not None and target_balance > 0
                else settings.sizing.portfolio_ratio
            )
            our_size = target_size * ratio
        else:
            our_size = target_size

        leverage = calculate_adjusted_leverage(
            target_leverage, settings.leverage.adjustment_ratio, symbol
        )
        # Apply the same hard exposure and margin ceilings used by fill copies
        # before sending a live resting order.
        max_size = settings.sizing.max_position_size / price
        if not reduce_only:
            if settings.simulated_trading:
                available_margin = simulated_balance
            else:
                follower_dex_state = await client.get_user_state(
                    settings.hyperliquid.wallet_address,
                    dex=perp_dex_for_symbol(symbol),
                )
                if follower_dex_state is None:
                    await _notify_copy_failure(
                        symbol=symbol, direction=position_side.value, target_size=target_size,
                        follower_size=our_size, price=price, category="DEX账户查询失败",
                        reason=f"Unable to read {perp_dex_for_symbol(symbol) or 'default'} DEX state",
                        fill_id=target_oid,
                        stage="镜像交易",
                    )
                    return
                available_margin = max(0.0, follower_dex_state.available_balance)
            max_size = min(
                max_size,
                available_margin * leverage * settings.copy_rules.max_margin_usage_ratio / price,
            )
        our_size = min(our_size, max_size)
        if our_size * price < MIN_POSITION_SIZE_USD:
            await _notify_copy_failure(
                symbol=symbol, direction=position_side.value, target_size=target_size,
                follower_size=our_size, price=price, category="低于最小订单金额",
                reason=f"Mirrored order notional ${our_size * price:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}",
                fill_id=target_oid,
                stage="镜像交易",
            )
            return

        result = await executor.execute_limit_order(
            symbol=symbol,
            side=OrderSide.BUY if is_buy else OrderSide.SELL,
            size=Decimal(str(our_size)),
            price=Decimal(str(price)),
            leverage=leverage,
            reduce_only=reduce_only,
        )
        if not result:
            await _notify_copy_failure(
                symbol=symbol, direction=position_side.value, target_size=target_size,
                follower_size=our_size, price=price, category="镜像挂单失败",
                reason=getattr(executor, "last_error", None) or "Executor returned no order id",
                fill_id=target_oid,
                stage="镜像交易",
            )
            return

        mirrored_orders[target_oid] = {
            "follower_oid": str(result), "symbol": symbol, "size": our_size,
            "price": price, "target_price": price, "target_size": target_size,
            "side": position_side.value, "leverage": leverage,
            "reduce_only": reduce_only,
            "active": True, "target_filled_size": 0.0,
        }
        logger.success(
            f"Mirrored target order: {symbol} {position_side.value} "
            f"target_oid={target_oid} follower_oid={result} size={our_size:.8f} price=${price:,.4f}"
        )
        if notifier and not order_data.get('_startup_snapshot'):
            await notifier.send_order_detected_notification(
                symbol=symbol, side=notification_side, size=our_size,
                entry_price=price, leverage=leverage, target_size=target_size,
                status="已镜像", target_leverage=target_leverage,
            )
    except Exception as e:
        logger.error(f"Error notifying new target order: {e}")
        try:
            await _notify_copy_failure(
                symbol=str(order_data.get("coin", "unknown")),
                direction=str(order_data.get("side", "unknown")),
                target_size=abs(float(order_data.get("sz", order_data.get("origSz", 0)) or 0)),
                follower_size=0,
                price=float(order_data.get("limitPx", order_data.get("px", 0)) or 0),
                category="镜像挂单异常",
                reason=str(e),
                fill_id=str(order_data.get("_target_oid", order_data.get("oid", "")) or ""),
                stage="镜像交易",
            )
        except Exception as notify_exc:
            logger.error(f"Unable to send mirror failure notification: {notify_exc}")


async def on_order_cancel(order_data: dict):
    """Cancel the still-resting follower order for a target terminal cancel."""
    target_oid = str(order_data.get("_target_oid", order_data.get("oid", "")) or "")
    mirror = mirrored_orders.get(target_oid)
    if not mirror or not mirror.get("active"):
        return
    if not settings.copy_rules.cancel_mirrored_orders:
        return
    try:
        cancelled = await executor.cancel_order(mirror["symbol"], mirror["follower_oid"])
        if cancelled:
            mirror["active"] = False
            logger.info(
                f"Cancelled mirrored order after target cancellation: "
                f"target_oid={target_oid} follower_oid={mirror['follower_oid']}"
            )
        elif any(token in str(getattr(executor, "last_error", "")).lower() for token in ("not found", "already filled", "does not exist", "order was filled")):
            # The follower order may have filled before the target cancellation
            # arrived. There is no resting order left to cancel, and the filled
            # position must remain untouched.
            mirror["active"] = False
            logger.info(f"Follower mirror already filled/absent for target oid={target_oid}; no position reversal")
        else:
            await _notify_copy_failure(
                symbol=mirror["symbol"], direction="cancel", target_size=0,
                follower_size=mirror.get("size", 0), price=mirror.get("price", 0),
                category="联动撤单失败",
                reason=getattr(executor, "last_error", None) or "Follower cancel rejected",
                fill_id=target_oid,
                stage="镜像交易",
            )
    except Exception as exc:
        await _notify_copy_failure(
            symbol=mirror["symbol"], direction="cancel", target_size=0,
            follower_size=mirror.get("size", 0), price=mirror.get("price", 0),
            category="联动撤单异常", reason=str(exc), fill_id=target_oid,
            stage="镜像交易",
        )


async def on_order_update(order_data: dict):
    """Track target updates and replace a mirrored order when its price changes."""
    target_oid = str(order_data.get("_target_oid", order_data.get("oid", "")) or "")
    mirror = mirrored_orders.get(target_oid)
    if mirror:
        status = str(order_data.get("_order_status", "") or "").lower()
        if status:
            mirror["target_status"] = status
        target_price = float(order_data.get("limitPx", order_data.get("px", 0)) or 0)
        if (
            settings.copy_rules.mirror_order_price
            and
            status in {"open", "resting"}
            and target_price > 0
            and abs(target_price - float(mirror.get("target_price", mirror.get("price", 0)))) > 1e-12
            and not mirror.get("replacing")
        ):
            mirror["replacing"] = True
            try:
                if not settings.copy_rules.cancel_mirrored_orders:
                    logger.warning(
                        f"Target order {target_oid} changed price, but mirrored cancellation is disabled; "
                        "keeping the original follower order"
                    )
                    return
                cancelled = await executor.cancel_order(mirror["symbol"], mirror["follower_oid"])
                if not cancelled:
                    await _notify_copy_failure(
                        symbol=mirror["symbol"], direction="replace", target_size=mirror.get("target_size", 0),
                        follower_size=mirror.get("size", 0), price=target_price,
                        category="改单撤旧失败",
                        reason=getattr(executor, "last_error", None) or "Follower cancel rejected",
                        fill_id=target_oid,
                        stage="镜像交易",
                    )
                    return
                mirrored_orders.pop(target_oid, None)
                await on_new_order(order_data)
            finally:
                replacement = mirrored_orders.get(target_oid)
                if replacement:
                    replacement["target_price"] = target_price
                else:
                    mirror["replacing"] = False
            return


async def _legacy_on_order_fill(fill_data: dict):
    """
    Called when an order is filled
    Copy the filled order
    """
    global trades_copied_count, is_paused, simulated_balance, simulated_positions, simulated_pnl
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping fill copy")
        return
    
    try:
        symbol = fill_data.get('coin', '')
        side_str = fill_data.get('side', '')  # 'B' for buy, 'S' for sell
        target_size = abs(float(fill_data.get('sz', 0)))
        price = float(fill_data.get('px', 0))
        direction = fill_data.get('dir', '')  # e.g., "Open Long", "Close Short"
        crossed = fill_data.get('crossed', False)  # True if crossed the spread (maker), False if took liquidity (taker)
        
        # Determine if this was likely a market or limit order
        # If crossed=False, it's typically a market order (taker)
        # If crossed=True, it's typically a limit order that got filled (maker)
        order_type = "LIMIT" if crossed else "MARKET"
        
        # Convert side to PositionSide
        if "Long" in direction:
            position_side = PositionSide.LONG
        elif "Short" in direction:
            position_side = PositionSide.SHORT
        else:
            # Fallback: Use side indicator
            position_side = PositionSide.LONG if side_str == "B" else PositionSide.SHORT
        
        logger.info("")
        logger.info(f"{'='*50}")
        logger.info(f"📋 FILL DETECTED!")
        logger.info(f"{'='*50}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Side: {position_side.value.upper()}")
        logger.info(f"Direction: {direction}")
        logger.info(f"Target Order Type: {order_type}")
        logger.info(f"Target Size: {target_size}")
        logger.info(f"Price: ${price:,.4f}")
        
        # Check if this is a position OPEN/ADD or CLOSE/REDUCE
        is_closing_reducing = "Close" in direction or "Reduce" in direction
        
        if is_closing_reducing:
            logger.warning(f"⚠️ Target is CLOSING/REDUCING position - NOT copying")
            logger.warning(f"   Reason: You likely don't have this position to close")
            logger.warning(f"   Direction: {direction}")
            return
        
        # Determine if this is a position-creating event
        is_position_flip = ">" in direction  # e.g., "Short > Long" or "Long > Short"
        is_opening = "Open" in direction or "Add" in direction
        is_closing_only = "Close" in direction and not is_position_flip
        
        logger.info(f"📌 Direction analysis: '{direction}'")
        logger.info(f"   - Position flip: {is_position_flip}")
        logger.info(f"   - Opening/Adding: {is_opening}")
        logger.info(f"   - Closing only: {is_closing_only}")
        
        # Get target position to calculate our size
        target_position = None
        if monitor.current_state:
            logger.debug(f"📊 Current cached positions: {len(monitor.current_state.positions)}")
            for pos in monitor.current_state.positions:
                logger.debug(f"   - {pos.symbol}: size={pos.size}")
                if pos.symbol == symbol:
                    target_position = pos
                    break
        
        # If no position found and this is a closing-only trade, skip it
        if not target_position and is_closing_only:
            logger.warning(f"⚠️ No position found for {symbol} in target wallet")
            logger.warning(f"   This appears to be a closing-only trade - skipping")
            return
        
        # If no position found but this is a flip or opening trade, retry after delay
        if not target_position and (is_position_flip or is_opening):
            logger.warning(f"⚠️ No position found for {symbol} - may be timing issue")
            logger.info(f"⏱️  Waiting 1.5 seconds and retrying position query...")
            
            # Wait for exchange to update
            await asyncio.sleep(1.5)
            
            # Refresh state
            await monitor.get_current_state()
            
            # Retry finding position
            if monitor.current_state:
                for pos in monitor.current_state.positions:
                    if pos.symbol == symbol:
                        target_position = pos
                        logger.success(f"✅ Position found after retry: {symbol}")
                        break
            
            if not target_position:
                logger.error(f"❌ Still no position found for {symbol} after retry")
                logger.error(f"   Direction: {direction}")
                logger.error(f"   This may indicate an exchange delay or position was immediately closed")
                return
        
        # Calculate our fill size
        # In live mode, use the follower wallet balance rather than the
        # simulated balance used by the dry-run account tracker.
        follower_balance = simulated_balance
        if not settings.simulated_trading:
            follower_state = await client.get_user_state(settings.hyperliquid.wallet_address)
            if follower_state is None:
                logger.error("Unable to read follower wallet state; skipping fill")
                return
            follower_balance = follower_state.balance

        our_size = position_sizer.calculate_size(
            target_position=target_position,
            target_wallet_balance=monitor.current_state.balance if monitor.current_state else 1000000,
            your_wallet_balance=follower_balance
        )
        
        if not our_size:
            logger.warning(f"⚠️ Skipping fill - size calculation returned None")
            return
        
        # Check minimum position size (Hyperliquid requirement)
        our_position_value = our_size * price
        if our_position_value < MIN_POSITION_SIZE_USD:
            logger.warning("")
            logger.warning(f"⚠️  Skipping Fill: {symbol}")
            logger.warning(f"   Position value ${our_position_value:.2f} below Hyperliquid minimum ${MIN_POSITION_SIZE_USD:.2f}")
            return
        
        logger.info("")
        logger.info(f"📊 Fill Sizing:")
        logger.info(f"   Target Size: {target_size}")
        logger.info(f"   Our Size: {our_size:.4f}")
        
        # Get target leverage
        target_leverage = target_position.leverage if target_position else 1.0
        
        # Adjust leverage with proper rounding and max limits
        our_leverage = calculate_adjusted_leverage(
            target_leverage=target_leverage,
            adjustment_ratio=settings.leverage.adjustment_ratio,
            symbol=symbol
        )
        
        logger.info(f"   Target Leverage: {target_leverage}x")
        logger.info(f"   Our Leverage: {our_leverage}x")
        
        # Determine order type based on settings
        use_limit = settings.copy_rules.use_limit_orders
        
        if use_limit:
            logger.info(f"   Order Type: LIMIT @ ${price:,.4f}")
        else:
            logger.info(f"   Order Type: MARKET")
        
        # Execute the order
        if use_limit:
            # Place limit order at the fill price
            result = await executor.execute_limit_order(
                symbol=symbol,
                side=OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL,
                size=our_size,
                price=price,
                leverage=our_leverage
            )
        else:
            # Place market order (original behavior)
            result = await executor.execute_market_order(
                symbol=symbol,
                side=OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL,
                size=our_size,
                leverage=our_leverage
            )
        
        if result:
            logger.success(f"✅ Fill copied successfully!")
            trades_copied_count += 1
            
            # Update simulated position
            if settings.simulated_trading:
                position_value = our_size * price
                margin_required = position_value / our_leverage
                
                if symbol not in simulated_positions:
                    simulated_positions[symbol] = {
                        'size': 0,
                        'entry_price': 0,
                        'leverage': our_leverage,
                        'side': position_side.value
                    }
                
                pos = simulated_positions[symbol]
                
                # Update position based on direction
                if "Open" in direction:
                    # Opening new position or adding to existing
                    total_value = (abs(pos['size']) * pos['entry_price']) + position_value
                    new_size = abs(pos['size']) + our_size
                    pos['entry_price'] = total_value / new_size if new_size > 0 else price
                    pos['size'] = new_size if position_side == PositionSide.LONG else -new_size
                    pos['side'] = position_side.value
                elif "Close" in direction:
                    # Closing position
                    pos['size'] = abs(pos['size']) - our_size
                    if position_side == PositionSide.SHORT:
                        pos['size'] = -pos['size']
                    if abs(pos['size']) < 0.0001:  # Effectively zero
                        del simulated_positions[symbol]
                        logger.info(f"   Position {symbol} closed")
                
                logger.success("")
                logger.success(f"💰 SIMULATED FILL EXECUTED!")
                logger.success(f"   Position: {symbol}")
                if symbol in simulated_positions:
                    logger.success(f"   New Size: {simulated_positions[symbol]['size']:.4f}")
                    logger.success(f"   Entry Price: ${simulated_positions[symbol]['entry_price']:.2f}")
                logger.success(f"   Margin Used: ${margin_required:,.2f}")
                logger.success(f"   Account Balance: ${simulated_balance:,.2f}")
            
            # Send notification
            if notifier:
                await notifier.send_trade_notification(
                    symbol=symbol,
                    side=OrderSide.BUY.value if position_side == PositionSide.LONG else OrderSide.SELL.value,
                    size=our_size,
                    entry_price=price,
                    leverage=our_leverage,
                    target_size=target_size
                )
        else:
            logger.error(f"❌ Failed to copy fill")
            
    except Exception as e:
        logger.error(f"Error copying fill: {e}")
        import traceback
        logger.error(traceback.format_exc())


def _fill_id(fill_data: dict) -> str:
    """Return the exchange trade id, with a stable fallback for malformed events."""
    trade_id = fill_data.get("tid")
    if trade_id is not None:
        return str(trade_id)
    return ":".join(str(fill_data.get(key, "")) for key in ("coin", "time", "oid", "sz", "px"))


def _remember_fill(fill_id: str) -> None:
    processed_fill_ids.add(fill_id)
    if len(processed_fill_ids) > MAX_PROCESSED_FILL_IDS:
        processed_fill_ids.clear()


def _target_position(symbol: str):
    if not monitor or not monitor.current_state:
        return None
    return next((position for position in monitor.current_state.positions if position.symbol == symbol), None)


async def _notify_copy_failure(
    *,
    symbol: str,
    direction: str,
    target_size: float,
    follower_size: float = 0.0,
    price: float = 0.0,
    category: str,
    reason: str,
    fill_id: str = "",
    stage: str = "成交跟单",
) -> None:
    """Send a best-effort Telegram diagnosis without masking the original failure."""
    logger.warning(
        f"Copy failure [{category}] {symbol} {direction}: {reason} "
        f"target={target_size:.8f} follower={follower_size:.8f}"
    )
    if notifier:
        try:
            await notifier.send_copy_failure_notification(
                symbol=symbol,
                side=direction or "unknown",
                target_size=target_size,
                follower_size=follower_size,
                price=price,
                category=category,
                reason=reason,
                fill_id=fill_id,
                stage=stage,
            )
        except Exception as exc:
            logger.error(f"Unable to send copy failure notification: {exc}")


async def on_order_fill(fill_data: dict):
    """Copy one target fill using its actual filled quantity.

    Target orders can be split into many fills. Copying the target's current
    position on each event compounds exposure, so all sizing starts from
    ``fill_data['sz']`` and each exchange trade id is handled at most once.
    """
    global trades_copied_count

    target_oid = str(fill_data.get("oid", "") or "")
    mirror = mirrored_orders.get(target_oid)
    if mirror:
        # The follower already has a resting limit order for this target oid.
        # Its own fill event changes the follower position; submitting a new
        # market/limit order here would double the exposure.
        target_fill_size = abs(float(fill_data.get("sz", 0) or 0))
        mirror["target_filled_size"] = mirror.get("target_filled_size", 0.0) + target_fill_size
        if settings.simulated_trading and target_fill_size > 0:
            # A dry-run has no follower exchange stream, so project the
            # proportional filled quantity into the local simulated account.
            target_order_size = max(float(mirror.get("target_size", 0)), target_fill_size)
            follower_fill_size = target_fill_size * float(mirror.get("size", 0)) / target_order_size
            symbol = mirror["symbol"]
            side = mirror.get("side", "long")
            direction = str(fill_data.get("dir", ""))
            position = simulated_positions.get(symbol)
            is_close = mirror.get("reduce_only") or "Close" in direction or "Reduce" in direction
            if is_close:
                if position:
                    remaining = max(0.0, abs(position["size"]) - follower_fill_size)
                    if remaining <= 1e-12:
                        simulated_positions.pop(symbol, None)
                    else:
                        position["size"] = remaining if position["size"] > 0 else -remaining
            else:
                price = float(fill_data.get("px", mirror.get("price", 0)) or mirror.get("price", 0))
                if position is None:
                    simulated_positions[symbol] = {
                        "size": follower_fill_size if side == "long" else -follower_fill_size,
                        "entry_price": price,
                        "side": side,
                        "leverage": mirror.get("leverage", 1),
                        "value": follower_fill_size * price,
                        "margin_used": follower_fill_size * price / max(float(mirror.get("leverage", 1)), 1),
                    }
                else:
                    prior_size = abs(position["size"])
                    total_size = prior_size + follower_fill_size
                    position["entry_price"] = (
                        prior_size * position["entry_price"] + follower_fill_size * price
                    ) / total_size
                    position["size"] = total_size if position["size"] > 0 else -total_size
        trades_copied_count += 1
        logger.info(
            f"Target fill acknowledged for mirrored order {target_oid}; "
            "no duplicate follower order will be submitted"
        )
        _remember_fill(_fill_id(fill_data))
        return

    if is_paused:
        logger.warning("Bot is paused - skipping fill copy")
        if notifier:
            await notifier.send_copy_failure_notification(
                symbol=str(fill_data.get("coin", "unknown")), side=str(fill_data.get("dir", "unknown")),
                target_size=abs(float(fill_data.get("sz", 0) or 0)), follower_size=0,
                price=float(fill_data.get("px", 0) or 0), category="机器人已暂停",
                reason="Bot is paused", fill_id=_fill_id(fill_data), stage="成交跟单"
            )
        return

    try:
        symbol = fill_data.get("coin", "")
        direction = fill_data.get("dir", "")
        target_size = abs(float(fill_data.get("sz", 0)))
        price = float(fill_data.get("px", 0))
        fill_id = _fill_id(fill_data)

        if fill_id in processed_fill_ids:
            logger.info(f"Skipping already copied fill {fill_id}")
            return
        if not symbol or target_size <= 0 or price <= 0:
            logger.warning(f"Skipping malformed fill: {fill_data}")
            return
        if ">" in direction:
            logger.warning(f"Skipping position flip until it can be reconciled safely: {direction}")
            return

        is_closing = "Close" in direction or "Reduce" in direction
        is_opening = "Open" in direction or "Add" in direction
        if not (is_opening or is_closing):
            logger.warning(f"Skipping unknown fill direction: {direction}")
            return

        if "Long" in direction:
            position_side = PositionSide.LONG
        elif "Short" in direction:
            position_side = PositionSide.SHORT
        else:
            position_side = PositionSide.LONG if fill_data.get("side") == "B" else PositionSide.SHORT

        follower_state = None
        follower_dex_state = None
        follower_balance = simulated_balance
        if not settings.simulated_trading:
            follower_state = await client.get_user_state(settings.hyperliquid.wallet_address)
            if follower_state is None:
                logger.error("Unable to read follower wallet state; skipping fill")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           price=price, category="账户查询失败", reason="Unable to read follower wallet state",
                                           fill_id=fill_id)
                return
            follower_balance = follower_state.balance
            follower_dex_state = await client.get_user_state(
                settings.hyperliquid.wallet_address,
                dex=perp_dex_for_symbol(symbol),
            )
            if follower_dex_state is None:
                logger.error(f"Unable to read {perp_dex_for_symbol(symbol) or 'default'} DEX state; skipping fill")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           price=price, category="DEX账户查询失败",
                                           reason=f"Unable to read {perp_dex_for_symbol(symbol) or 'default'} DEX state",
                                           fill_id=fill_id)
                return

        target_position = _target_position(symbol)
        if target_position is None and is_opening and monitor:
            # The fill is the source of truth, while the REST position
            # snapshot can lag the WebSocket event by a few hundred ms.
            # Refresh once before deciding that an opening fill is invalid.
            logger.info(
                f"Target position snapshot missing for opening fill {symbol}; "
                "refreshing before copy"
            )
            await monitor.get_current_state()
            target_position = _target_position(symbol)
        if is_closing:
            if settings.simulated_trading:
                simulated_position = simulated_positions.get(symbol)
                follower_size = abs(simulated_position["size"]) if simulated_position else 0
                follower_side = simulated_position.get("side") if simulated_position else None
            else:
                follower_position = next(
                    (position for position in follower_dex_state.positions if position.symbol == symbol), None
                )
                follower_size = follower_position.size if follower_position else 0
                follower_side = follower_position.side.value if follower_position else None

            if follower_size <= 0 or follower_side != position_side.value:
                logger.info(f"No matching follower {position_side.value} position for {symbol}; skipping reduce-only fill")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           price=price, category="没有匹配的跟随仓位",
                                           reason=f"Follower position is absent or side is {follower_side}; reduce-only close skipped",
                                           fill_id=fill_id)
                return

            target_remaining_size = float(
                fill_data.get("_target_remaining_size_after_fill", 0.0)
            )
            target_fully_closed = (
                "_target_remaining_size_after_fill" in fill_data
                and target_remaining_size <= 1e-12
            )
            if (
                "_target_remaining_size_after_fill" not in fill_data
                and target_position is not None
                and target_position.side == position_side
            ):
                target_remaining_size = target_position.size

            our_size, close_ratio = calculate_proportional_close_size(
                target_fill_size=target_size,
                target_remaining_size=target_remaining_size,
                follower_size=follower_size,
            )
            if our_size <= 0:
                logger.warning(f"Cannot calculate proportional close size for {symbol}; skipping safely")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           follower_size=follower_size, price=price, category="比例计算失败",
                                           reason="Calculated proportional close size is zero", fill_id=fill_id)
                return
            logger.info(
                f"Proportional close for {symbol}: target closed {close_ratio:.2%}; "
                f"follower closes {our_size:.8f} of {follower_size:.8f}"
            )
            # A partial close below the exchange minimum must wait for more
            # fills from the same target order. Once the target is fully flat,
            # force the final reduce-only order so the follower cannot retain
            # a residual position indefinitely.
            if our_size * price < MIN_POSITION_SIZE_USD and not target_fully_closed:
                logger.info(f"Reduce-only residual for {symbol} is below ${MIN_POSITION_SIZE_USD:.2f}; leaving it open")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           follower_size=our_size, price=price, category="低于最小订单金额",
                                           reason=f"Reduce-only notional ${our_size * price:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}; waiting for more fills",
                                           fill_id=fill_id)
                return

            order_side = OrderSide.SELL if position_side == PositionSide.LONG else OrderSide.BUY
            leverage = 1
            result = await executor.execute_market_order(
                symbol=symbol,
                side=order_side,
                size=Decimal(str(our_size)),
                reduce_only=True,
            )
        else:
            target_balance = monitor.current_state.balance if monitor and monitor.current_state else 0
            if settings.copy_rules.auto_adjust_size:
                if target_balance <= 0:
                    logger.error("Target balance is unavailable; skipping fill")
                    await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                               price=price, category="目标账户余额无效", reason="Target balance is zero or unavailable",
                                               fill_id=fill_id)
                    return
                our_size = target_size * (follower_balance / target_balance)
            else:
                our_size = target_size

            if our_size <= 0:
                logger.warning("Skipping fill because calculated size is zero")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           price=price, category="跟单数量为零", reason="Calculated follower size is zero",
                                           fill_id=fill_id)
                return

            target_leverage = target_position.leverage if target_position else 1.0
            if target_position is None:
                logger.warning(
                    f"Target position still unavailable for opening fill {symbol}; "
                    "copying with conservative 1x leverage"
                )
            leverage = calculate_adjusted_leverage(
                target_leverage=target_leverage,
                adjustment_ratio=settings.leverage.adjustment_ratio,
                symbol=symbol,
            )
            if settings.simulated_trading:
                follower_position = simulated_positions.get(symbol)
                available_margin = simulated_balance
                open_positions = len(simulated_positions)
            else:
                follower_position = next(
                    (position for position in follower_dex_state.positions if position.symbol == symbol), None
                )
                available_margin = max(0.0, follower_dex_state.available_balance)
                open_positions = len(follower_state.positions)

                if available_margin <= 0:
                    dex_name = perp_dex_for_symbol(symbol) or "default"
                    logger.warning(
                        f"Skipping {symbol}: {dex_name} Perp DEX available margin is $0.00. "
                        f"Fund the {dex_name} DEX before copying this asset."
                    )
                    await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                               follower_size=our_size, price=price, category="保证金不足",
                                               reason=f"{dex_name} available margin is ${available_margin:.2f}", fill_id=fill_id)
                    return

            if (
                settings.copy_rules.max_open_trades is not None
                and follower_position is None
                and open_positions >= settings.copy_rules.max_open_trades
            ):
                logger.warning(f"Max open trades limit reached ({open_positions}/{settings.copy_rules.max_open_trades}); skipping {symbol}")
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           follower_size=our_size, price=price, category="达到最大持仓数",
                                           reason=f"Open positions {open_positions} >= limit {settings.copy_rules.max_open_trades}", fill_id=fill_id)
                return

            max_size_from_margin = (
                available_margin
                * leverage
                * settings.copy_rules.max_margin_usage_ratio
            ) / price
            existing_position_value = follower_position.notional_value if follower_position else 0.0
            remaining_position_value = max(
                0.0, settings.sizing.max_position_size - existing_position_value
            )
            max_size_from_position_limit = remaining_position_value / price
            our_size = min(our_size, max_size_from_margin, max_size_from_position_limit)
            if our_size * price < MIN_POSITION_SIZE_USD:
                logger.warning(
                    f"Skipping {symbol}: copied fill value ${our_size * price:.2f} is below "
                    f"${MIN_POSITION_SIZE_USD:.2f} after margin/MAX_POSITION_SIZE limits"
                )
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           follower_size=our_size, price=price, category="低于最小订单金额",
                                           reason=f"Follower notional ${our_size * price:.2f} is below ${MIN_POSITION_SIZE_USD:.2f} after risk limits",
                                           fill_id=fill_id)
                return

            order_side = OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL
            if settings.copy_rules.use_limit_orders:
                result = await executor.execute_limit_order(
                    symbol=symbol,
                    side=order_side,
                    size=Decimal(str(our_size)),
                    price=Decimal(str(price)),
                    leverage=leverage,
                )
            else:
                result = await executor.execute_market_order(
                    symbol=symbol,
                    side=order_side,
                    size=Decimal(str(our_size)),
                    leverage=leverage,
                )

        if not result:
            executor_reason = getattr(executor, "last_error", None) or "Executor returned no order id"
            logger.error(
                f"Failed to copy fill {fill_id}: executor returned no order id; "
                f"symbol={symbol} direction={direction} size={our_size:.8f} "
                f"notional=${our_size * price:.2f} simulated={settings.simulated_trading}"
            )
            await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                       follower_size=our_size, price=price, category="交易所拒绝或执行器失败",
                                       reason=executor_reason, fill_id=fill_id)
            return

        _remember_fill(fill_id)
        trades_copied_count += 1
        logger.success(
            f"Fill copied: {symbol} {order_side.value} size={our_size:.8f} "
            f"target_size={target_size:.8f} reduce_only={is_closing}"
        )

        if settings.simulated_trading:
            position = simulated_positions.get(symbol)
            if is_opening:
                if position is None:
                    position = {"size": 0.0, "entry_price": 0.0, "side": position_side.value}
                    simulated_positions[symbol] = position
                previous_size = abs(position["size"])
                total_size = previous_size + our_size
                position["entry_price"] = (
                    ((previous_size * position["entry_price"]) + (our_size * price)) / total_size
                )
                position["size"] = total_size if position_side == PositionSide.LONG else -total_size
                position["side"] = position_side.value
            elif position is not None:
                remaining = max(0.0, abs(position["size"]) - our_size)
                if remaining == 0:
                    del simulated_positions[symbol]
                else:
                    position["size"] = remaining if position_side == PositionSide.LONG else -remaining

        if notifier:
            notification_sent = await notifier.send_trade_notification(
                symbol=symbol,
                side=order_side.value,
                size=our_size,
                entry_price=price,
                leverage=leverage,
                target_size=target_size,
                is_simulated=settings.simulated_trading,
            )
            if not notification_sent:
                logger.error(
                    f"Copy succeeded but Telegram notification failed for {fill_id}"
                )
    except Exception as exc:
        logger.exception(f"Error copying fill: {exc}")
        await _notify_copy_failure(
            symbol=str(fill_data.get("coin", "unknown")),
            direction=str(fill_data.get("dir", "unknown")),
            target_size=abs(float(fill_data.get("sz", 0) or 0)),
            price=float(fill_data.get("px", 0) or 0),
            category="程序异常", reason=str(exc), fill_id=_fill_id(fill_data)
        )


# Telegram bot callback functions
async def get_status() -> str:
    """Get current bot status for Telegram"""
    uptime = (datetime.now(SHANGHAI_TZ) - bot_start_time).total_seconds() / 3600 if bot_start_time else 0
    
    follower_state = None if settings.simulated_trading else await _get_follower_state()
    
    if settings.simulated_trading:
        balance = simulated_balance
        pnl = simulated_pnl
    else:
        balance = follower_state.balance if follower_state else 0
        pnl = follower_state.unrealized_pnl if follower_state else 0
    
    status_emoji = "🟢" if not is_paused else "⏸️"
    status_text = "运行中" if not is_paused else "已暂停"
    mode = "模拟" if settings.simulated_trading else "实盘"
    
    return f"""
📊 <b>跟单运行状态</b>

{status_emoji} <b>状态：</b>{status_text}
🎮 <b>模式：</b>{mode}
👤 <b>目标：</b><code>{settings.target_wallet[:10]}...{settings.target_wallet[-6:]}</code>
💼 <b>账户余额：</b>${balance:,.2f}
📈 <b>未实现盈亏：</b>${pnl:,.2f}
📊 <b>已复制成交：</b>{trades_copied_count}
📍 <b>持仓数：</b>{len(simulated_positions) if settings.simulated_trading else (len(follower_state.positions) if follower_state else 0)}
⏰ <b>运行时长：</b>{uptime:.1f} 小时

<b>仓位模式：</b>{settings.sizing.mode.title()}
<b>杠杆：</b>目标杠杆的 {settings.leverage.adjustment_ratio} 倍
    """.strip()


async def get_positions() -> list:
    """Get positions from the follower account for Telegram."""
    state = await _get_follower_state() if not settings.simulated_trading else None
    if settings.simulated_trading:
        return [
            {
                "symbol": symbol,
                "size": position.get("size", 0),
                "entry_price": position.get("entry_price", 0),
                "current_price": position.get("entry_price", 0),
                "unrealized_pnl": 0,
                "leverage": position.get("leverage", 1),
            }
            for symbol, position in simulated_positions.items()
        ]
    if not state:
        return []
    positions = []
    for pos in state.positions:
        positions.append({
            'symbol': pos.symbol,
            'size': pos.size,
            'entry_price': pos.entry_price,
            'current_price': pos.current_price,
            'unrealized_pnl': pos.unrealized_pnl,
            'leverage': pos.leverage
        })
    
    return positions


async def get_orders() -> list:
    """Get open orders from the follower account for Telegram."""
    state = await _get_follower_state() if not settings.simulated_trading else None
    if not state:
        return []
    
    orders = []
    for order in state.orders:
        orders.append({
            'symbol': order.symbol,
            'side': order.side,
            'size': order.size,
            'price': order.price,
            'order_type': order.order_type,
            'trigger_price': getattr(order, 'trigger_price', None)
        })
    
    return orders


async def _get_follower_state():
    """Read the account that receives copied orders, never the target account."""
    if settings.simulated_trading or not client or not settings.hyperliquid.wallet_address:
        return None
    return await client.get_user_state(settings.hyperliquid.wallet_address)


def _format_performance(performance: dict) -> str:
    lines = []
    for label in ("24H", "7D", "30D"):
        item = performance.get(label) if performance else None
        if not item:
            lines.append(f"• {label}：暂无数据")
            continue
        change = item["change"]
        emoji = "📈" if change >= 0 else "📉"
        lines.append(f"• {label}：{emoji} ${change:+,.2f} ({item['change_pct']:+.2f}%)")
    return "\n".join(lines)


def _wallet_label(address: str) -> str:
    if not address:
        return "未配置"
    return f"{address[:10]}...{address[-6:]}"


async def get_pnl() -> str:
    """Get PnL for Telegram"""
    target_state = monitor.current_state if monitor else None
    follower_state = None
    
    if settings.simulated_trading:
        balance = simulated_balance
        equity = simulated_balance
        pnl = simulated_pnl
        mode = "模拟"
    else:
        follower_state = await _get_follower_state()
        balance = follower_state.balance if follower_state else 0
        equity = follower_state.total_equity if follower_state else 0
        pnl = follower_state.unrealized_pnl if follower_state else 0
        mode = "实盘"

    target_performance = await client.get_portfolio_performance(settings.target_wallet) if client else {}
    follower_performance = (
        await client.get_portfolio_performance(settings.hyperliquid.wallet_address)
        if client and not settings.simulated_trading and settings.hyperliquid.wallet_address else {}
    )
    target_line = _wallet_label(settings.target_wallet)
    follower_line = _wallet_label(settings.hyperliquid.wallet_address) if not settings.simulated_trading else "模拟账户"
    target_summary = (
        f"余额：${target_state.balance:,.2f}｜未实现盈亏：${target_state.unrealized_pnl:,.2f}"
        if target_state else "当前状态暂无数据"
    )
    history_note = "模拟模式不提供跟随账户链上历史" if settings.simulated_trading else "按账户净值计算，充值/提现会影响结果"
    return f"""
💰 <b>账户盈亏摘要</b>

🎮 <b>模式：</b>{mode}

<b>跟随钱包：</b><code>{follower_line}</code>
• 余额：${balance:,.2f}
• 权益：${equity:,.2f}
• 未实现盈亏：${pnl:,.2f}

<b>跟随钱包周期净值变化</b>
{_format_performance(follower_performance) if not settings.simulated_trading else '• 24H/7D/30D：模拟模式暂无链上历史'}

<b>目标钱包：</b><code>{target_line}</code>
• {target_summary}
<b>目标钱包周期净值变化</b>
{_format_performance(target_performance)}

<b>本次运行：</b>
• 已复制成交：{trades_copied_count}
• 跟随持仓数：{len(simulated_positions) if settings.simulated_trading else (len(follower_state.positions) if follower_state else 0)}

<i>{history_note}</i>
    """.strip()


async def get_positions_formatted() -> str:
    """Get current positions for Telegram"""
    state = await _get_follower_state() if not settings.simulated_trading else None
    if settings.simulated_trading:
        return "📍 <b>当前持仓（模拟跟随账户）</b>\n\n" + ("暂无持仓。" if not simulated_positions else "\n".join(
            f"• <b>{html.escape(symbol)}</b>：{position['size']:.4f}"
            for symbol, position in simulated_positions.items()
        ))
    
    if not state or not state.positions:
        return "📍 <b>当前持仓</b>\n\n暂无持仓。"
    
    message = f"📍 <b>当前持仓（{len(state.positions)}）</b>\n\n"
    
    for i, pos in enumerate(state.positions, 1):
        pnl_emoji = "📈" if pos.unrealized_pnl > 0 else "📉"
        message += f"""
{i}️⃣ <b>{html.escape(pos.symbol)}</b> {pos.side.value.upper()}
   数量：{pos.size:.4f}
   开仓价：${pos.entry_price:,.2f}
   当前价：${pos.current_price:,.2f}
   杠杆：{pos.leverage}x
   未实现盈亏：{pnl_emoji} ${pos.unrealized_pnl:,.2f} ({pos.pnl_percentage:+.2f}%)

"""
    
    return message.strip()


async def get_leaderboard(window: str, sort_by: str, page: int = 0) -> tuple[str, int]:
    """Format up to 200 public leaderboard rows in Telegram-sized pages."""
    labels = {"day": "24H", "week": "7D", "month": "30D"}
    sort_labels = {"pnl": "收益额", "roi": "收益率"}
    page_size = 10
    rows = await client.get_leaderboard(window, sort_by, limit=200) if client else []
    title = f"🏆 <b>Hyperliquid 收益排行榜</b>\n周期：{labels[window]}｜排序：{sort_labels[sort_by]}"
    if not rows:
        return title + "\n\n暂无数据。公开排行榜接口暂不可用或未返回该周期数据。", 0

    total_pages = (len(rows) + page_size - 1) // page_size
    page = min(max(page, 0), total_pages - 1)
    start = page * page_size
    page_rows = rows[start:start + page_size]

    lines = [f"{title}\n第 {page + 1}/{total_pages} 页｜前 {len(rows)} 名", ""]
    for index, row in enumerate(page_rows, start + 1):
        address = str(row.get("address", ""))
        name = row.get("name") or address or "未知地址"
        pnl = row.get("pnl")
        roi = row.get("roi")
        pnl_text = f"${pnl:+,.2f}" if pnl is not None else "暂无"
        roi_text = f"{roi:+.2f}%" if roi is not None else "暂无"
        lines.append(f"<b>{index}. {html.escape(str(name))}</b>")
        if row.get("name") and address:
            lines.append(f"   地址：<code>{html.escape(address)}</code>")
        lines.append(f"   收益：{pnl_text}｜收益率：{roi_text}")
    lines.append("\n<i>数据来自 Hyperliquid 公开排行榜，不代表跟随钱包收益。</i>")
    return "\n".join(lines), total_pages


async def get_wallet_report(address: str, fill_limit: int = 10) -> str:
    """Format public account performance and recent fills for Telegram."""
    if not client:
        return "❌ Hyperliquid 客户端尚未初始化。"

    state = await client.get_user_state(address)
    performance = await client.get_portfolio_performance(address)
    fills = await client.get_user_fills(address, fill_limit)
    if state:
        account_summary = (
            f"• 账户价值：${state.balance:,.2f}\n"
            f"• 未实现盈亏：${state.unrealized_pnl:,.2f}\n"
            f"• 当前持仓：{len(state.positions)}｜挂单：{len(state.orders)}"
        )
    else:
        account_summary = "• 当前账户状态暂不可用"

    lines = [
        "🔎 <b>Hyperliquid 公开账户查询</b>",
        f"<code>{html.escape(address)}</code>",
        "",
        "<b>账户状态</b>",
        account_summary,
        "",
        "<b>周期净值变化</b>",
        _format_performance(performance),
        "",
        f"<b>最近成交（{len(fills)} 笔）</b>",
    ]
    if not fills:
        lines.append("暂无可用成交记录。")
    else:
        for index, fill in enumerate(fills, 1):
            timestamp = (
                datetime.fromtimestamp(fill["timestamp"] / 1000, timezone.utc)
                .astimezone(SHANGHAI_TZ)
                .strftime("%m-%d %H:%M UTC+8")
                if fill["timestamp"] else "时间未知"
            )
            direction = fill["direction"] or fill["side"] or "未知方向"
            lines.append(
                f"<b>{index}. {html.escape(fill['symbol'])}</b> {html.escape(direction)}\n"
                f"   数量：{fill['size']:,.6f}｜价格：${fill['price']:,.4f}\n"
                f"   时间：{timestamp}"
            )
            details = []
            if fill["closed_pnl"] is not None:
                details.append(f"已实现盈亏：${fill['closed_pnl']:+,.2f}")
            if fill["fee"] is not None:
                details.append(f"手续费：${fill['fee']:,.4f}")
            if details:
                lines.append("   " + "｜".join(details))

    lines.append("\n<i>净值变化包含充值/提现影响；最近成交按交易所 fills 返回，非按开平仓配对后的完整交易。</i>")
    return "\n".join(lines)


async def handle_pause():
    """Handle pause request from Telegram"""
    global is_paused
    is_paused = True
    logger.warning("⏸️ Bot paused by Telegram command")


async def handle_resume():
    """Handle resume request from Telegram"""
    global is_paused
    is_paused = False
    logger.info("▶️ Bot resumed by Telegram command")


async def handle_stop(close_positions: bool = False):
    """Handle stop request from Telegram"""
    logger.warning(f"🛑 Stop requested from Telegram (close_positions={close_positions})")
    
    # Cancel all orders
    if executor:
        await executor.cancel_all_orders()
    
    # Close positions if requested
    if close_positions and monitor and monitor.current_state:
        for pos in monitor.current_state.positions:
            logger.info(f"Closing position: {pos.symbol}")
            await executor.close_position(pos.symbol)
    
    # Stop monitoring
    if monitor:
        await monitor.stop_monitoring()
    
    # Stop Telegram bot
    if telegram_bot:
        await telegram_bot.stop()
    
    # Exit
    import sys
    sys.exit(0)


async def send_hourly_reports():
    """Send hourly reports via Telegram"""
    while True:
        try:
            await asyncio.sleep(3600)  # Wait 1 hour
            
            if notifier and monitor and monitor.current_state:
                state = await _get_follower_state() if not settings.simulated_trading else None
                if settings.simulated_trading:
                    account_pnl = simulated_pnl
                    account_balance = simulated_balance
                    open_positions = len(simulated_positions)
                    open_orders = 0
                elif not state:
                    continue
                else:
                    account_pnl = state.unrealized_pnl
                    account_balance = state.balance
                    open_positions = len(state.positions)
                    open_orders = len(state.orders)
                
                await notifier.send_hourly_report(
                    trades_copied=trades_copied_count,
                    account_pnl_usd=account_pnl,
                    account_pnl_pct=(account_pnl / account_balance * 100) if account_balance > 0 else 0,
                    open_positions=open_positions,
                    open_orders=open_orders,
                    target_wallet=settings.target_wallet
                )
        except Exception as e:
            logger.error(f"Error sending hourly report: {e}")

async def main():
    """
    Main entry point for the copy trading bot
    """
    global monitor, executor, position_sizer, client, telegram_bot, notifier, bot_start_time
    global simulated_balance, trades_copied_count
    
    bot_start_time = datetime.now(SHANGHAI_TZ)
    trades_copied_count = 0
    
    # Keep this variable as the account balance used by sizing and status
    # reporting. In live mode it is populated from the follower wallet below.
    simulated_balance = settings.simulated_account_balance
    
    logger.info("=" * 60)
    logger.info("🚀 Hyperliquid Copy Trading Bot Starting...")
    logger.info("=" * 60)
    
    if settings.simulated_trading:
        logger.warning("🎮 SIMULATED TRADING MODE")
        logger.warning(f"💰 Simulated Account Balance: ${simulated_balance:,.2f}")
    else:
        logger.warning("⚠️ LIVE TRADING MODE - REAL MONEY AT RISK!")
    
    target_address = settings.target_wallet
    logger.info(f"📍 Target Address: {target_address}")
    
    # Initialize components
    client = HyperliquidClient(
        settings.hyperliquid.api_url,
        settings.hyperliquid.leaderboard_url,
    )
    
    monitor = WalletMonitor(
        target_address,
        settings.hyperliquid.api_url,
        settings.hyperliquid.ws_url,
        fill_polling_enabled=settings.copy_rules.fill_polling_enabled,
        fill_poll_interval_seconds=settings.copy_rules.fill_poll_interval_seconds,
    )
    
    executor = TradeExecutor(
        wallet_address=settings.hyperliquid.wallet_address,
        private_key=settings.hyperliquid.private_key,
        info_url=settings.hyperliquid.api_url + "/info",
        exchange_url=settings.hyperliquid.api_url + "/exchange",
        dry_run=settings.simulated_trading,
        max_slippage_pct=settings.copy_rules.max_slippage_pct,
    )
    
    # Fetch target wallet state to auto-calculate ratio
    logger.info("")
    logger.info(f"📊 Fetching initial state...")
    state = await monitor.get_current_state()
    
    if state:
        target_balance = state.balance
        logger.info("")
        logger.info(f"💼 Target Account:")
        logger.info(f"   Balance: ${target_balance:,.2f}")
        logger.info(f"   Equity: ${state.total_equity:,.2f}")
        logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
        logger.info(f"   Open Positions: {len(state.positions)}")
        
        # In live mode, size against the follower wallet's actual balance.
        if not settings.simulated_trading:
            if not settings.hyperliquid.wallet_address or not settings.hyperliquid.private_key:
                raise RuntimeError("Live trading requires HYPERLIQUID_WALLET_ADDRESS and HYPERLIQUID_PRIVATE_KEY")
            follower_state = await client.get_user_state(settings.hyperliquid.wallet_address)
            if follower_state is None:
                raise RuntimeError("Unable to read follower wallet state")
            simulated_balance = follower_state.balance

            if simulated_balance <= 0:
                spot_balances = await client.get_spot_balances(settings.hyperliquid.wallet_address)
                spot_usdc = next(
                    (float(item.get("total", 0)) for item in spot_balances if item.get("coin") == "USDC"),
                    0.0,
                )
                raise RuntimeError(
                    f"Perp clearinghouse balance is ${simulated_balance:,.2f}. "
                    f"Spot USDC balance is ${spot_usdc:,.2f}. "
                    "Transfer USDC from Spot to Perp on Hyperliquid before live trading."
                )

        if target_balance <= 0:
            raise RuntimeError("Target account balance is zero; cannot calculate copy ratio")

        # Auto-calculate ratio based on balances
        auto_ratio = simulated_balance / target_balance
        settings.sizing.portfolio_ratio = auto_ratio
        
        logger.success("")
        logger.success(f"✨ AUTO-CALCULATED SIZING:")
        logger.success(f"   Target Balance: ${target_balance:,.2f}")
        logger.success(f"   Your Balance: ${simulated_balance:,.2f}")
        ratio_text = f"1:{int(1 / auto_ratio)}" if auto_ratio > 0 else "0 (no funds)"
        logger.success(f"   📊 Ratio: {ratio_text} ({auto_ratio*100:.4f}%)")
        if auto_ratio > 0:
            logger.success(f"   This means: For every ${int(1/auto_ratio)} target trades, you copy ${1}")
        
        # Calculate minimum balance needed for $10 minimum order size
        if state.positions:
            # Find smallest target position value
            smallest_target_value = min(abs(pos.size) * pos.entry_price for pos in state.positions)
            # Calculate minimum balance needed to copy at $10
            min_balance_needed = MIN_POSITION_SIZE_USD * (target_balance / smallest_target_value)
            
            logger.info("")
            logger.info(f"⚠️  MINIMUM BALANCE CHECK:")
            logger.info(f"   Hyperliquid Min Order Size: ${MIN_POSITION_SIZE_USD:.2f}")
            logger.info(f"   Smallest Target Position: ${smallest_target_value:,.2f}")
            logger.info(f"   Min Balance Needed (for this ratio): ${min_balance_needed:,.2f}")
            
            if simulated_balance < min_balance_needed:
                positions_below_min = sum(1 for pos in state.positions 
                                         if (abs(pos.size) * pos.entry_price * auto_ratio) < MIN_POSITION_SIZE_USD)
                logger.warning(f"   ⚠️  WARNING: Your balance ${simulated_balance:,.2f} is below recommended minimum!")
                logger.warning(f"   {positions_below_min} out of {len(state.positions)} positions will be SKIPPED (below $10)")
                logger.warning(f"   Consider increasing balance to ${min_balance_needed:,.2f} to copy all positions")
            else:
                logger.success(f"   ✅ Your balance is sufficient to copy all positions!")
        
        if state.positions:
            logger.info("")
            logger.info(f"📊 Current Positions:")
            logger.info(f"=" * 60)
            
            total_simulated_margin = 0
            for i, pos in enumerate(state.positions, 1):
                target_position_value = abs(pos.size) * pos.entry_price
                your_position_value = target_position_value * auto_ratio
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                your_leverage = calculate_adjusted_leverage(
                    target_leverage=pos.leverage,
                    adjustment_ratio=settings.leverage.adjustment_ratio,
                    symbol=pos.symbol
                )
                # Startup copying must use the same risk caps as live fills.
                # The exchange reserves margin and fees, so using the entire
                # displayed balance can be rejected as insufficient margin.
                if settings.simulated_trading:
                    available_margin = simulated_balance
                else:
                    current_follower_state = await client.get_user_state(
                        settings.hyperliquid.wallet_address,
                        dex=perp_dex_for_symbol(pos.symbol),
                    )
                    if current_follower_state is None:
                        logger.error(f"   ❌ Cannot read follower wallet before copying {pos.symbol}")
                        continue
                    available_margin = max(0.0, current_follower_state.available_balance)
                    if available_margin <= 0:
                        dex_name = perp_dex_for_symbol(pos.symbol) or "default"
                        logger.warning(
                            f"   ⚠️ {pos.symbol} belongs to the {dex_name} Perp DEX, "
                            "which has $0.00 available margin. No order will be sent."
                        )

                margin_limited_value = (
                    available_margin
                    * your_leverage
                    * settings.copy_rules.max_margin_usage_ratio
                )
                max_position_value = settings.sizing.max_position_size
                capped_position_value = min(
                    target_position_value * auto_ratio,
                    margin_limited_value,
                    max_position_value,
                )
                if capped_position_value < MIN_POSITION_SIZE_USD:
                    logger.warning(
                        f"⚠️  Skipping Position {i}/{len(state.positions)}: {pos.symbol}; "
                        f"allowed value ${capped_position_value:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}"
                    )
                    continue
                if capped_position_value < target_position_value * auto_ratio:
                    logger.warning(
                        f"⚠️  Capping {pos.symbol} startup copy from "
                        f"${target_position_value * auto_ratio:,.2f} to ${capped_position_value:,.2f} "
                        f"(available-margin or MAX_POSITION_SIZE limit)"
                    )
                your_position_value = capped_position_value
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                margin_needed = your_position_value / your_leverage
                total_simulated_margin += margin_needed
                
                logger.info("")
                logger.info(f"   Position {i}: {pos.symbol} {pos.side.value.upper()}")
                logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
                logger.info(f"   Target Value: ${target_position_value:,.2f}")
                logger.success(f"   → Your Copy: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
                logger.success(f"   → Your Value: ${your_position_value:,.2f}")
                logger.success(f"   → Margin Needed: ${margin_needed:,.2f}")
            
            logger.info("")
            logger.info("=" * 60)
            logger.warning(f"📊 If you copied all {len(state.positions)} positions:")
            logger.warning(f"   Total Margin Needed: ${total_simulated_margin:,.2f}")
            logger.warning(f"   Your Balance: ${simulated_balance:,.2f}")
            logger.warning(f"   Remaining: ${simulated_balance - total_simulated_margin:,.2f}")
            logger.info(f"=" * 60)
    
    logger.info(f"")
    logger.info(f"🔧 Copy Trading Settings:")
    logger.info(f"   Sizing Mode: {settings.sizing.mode}")
    logger.info(f"   Leverage Adjustment: {settings.leverage.adjustment_ratio}x")
    logger.info(f"   Max Position Size: ${settings.sizing.max_position_size:,.2f}")
    logger.info(
        f"   Max Margin Usage: {settings.copy_rules.max_margin_usage_ratio:.0%}"
    )
    logger.info(
        f"   Fill Polling Fallback: "
        f"{'enabled (' + str(settings.copy_rules.fill_poll_interval_seconds) + 's)' if settings.copy_rules.fill_polling_enabled else 'disabled'}"
    )
    logger.info(f"   Pending Order Mirror: {settings.copy_rules.mirror_pending_orders}")
    logger.info(f"   Cancel Mirrored Orders: {settings.copy_rules.cancel_mirrored_orders}")
    
    position_sizer = PositionSizer(
        mode=settings.sizing.mode,
        fixed_size=settings.sizing.fixed_size,
        portfolio_ratio=settings.sizing.portfolio_ratio,
        max_position_size=settings.sizing.max_position_size,
        max_total_exposure=settings.sizing.max_total_exposure
    )
    
    # Fills are the real-time source of truth. Position and order callbacks
    # describe the same target action and would otherwise duplicate an order.
    monitor.on_new_position = None
    monitor.on_position_close = None
    monitor.on_position_update = None
    monitor.on_new_order = on_new_order
    monitor.on_order_update = on_order_update
    monitor.on_order_cancel = on_order_cancel
    monitor.on_order_fill = on_order_fill
    
    # Copy existing positions if enabled
    if settings.copy_rules.copy_open_positions and state and state.positions:
        logger.info("=" * 60)
        logger.success("🔄 COPYING EXISTING POSITIONS ON STARTUP")
        logger.info("=" * 60)
        
        copied_count = 0
        for i, pos in enumerate(state.positions, 1):
            try:
                # Calculate your copy
                target_position_value = abs(pos.size) * pos.entry_price
                your_position_value = target_position_value * auto_ratio
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                your_leverage = calculate_adjusted_leverage(
                    target_leverage=pos.leverage,
                    adjustment_ratio=settings.leverage.adjustment_ratio,
                    symbol=pos.symbol
                )
                if settings.simulated_trading:
                    available_margin = simulated_balance
                else:
                    current_follower_state = await client.get_user_state(
                        settings.hyperliquid.wallet_address,
                        dex=perp_dex_for_symbol(pos.symbol),
                    )
                    if current_follower_state is None:
                        logger.error(f"   ❌ Cannot read follower wallet before copying {pos.symbol}")
                        continue
                    available_margin = max(0.0, current_follower_state.available_balance)
                    if available_margin <= 0:
                        dex_name = perp_dex_for_symbol(pos.symbol) or "default"
                        logger.warning(
                            f"   ⚠️ {pos.symbol} belongs to the {dex_name} Perp DEX, "
                            "which has $0.00 available margin. No order will be sent."
                        )

                requested_position_value = target_position_value * auto_ratio
                capped_position_value = min(
                    requested_position_value,
                    available_margin
                    * your_leverage
                    * settings.copy_rules.max_margin_usage_ratio,
                    settings.sizing.max_position_size,
                )
                if capped_position_value < MIN_POSITION_SIZE_USD:
                    logger.warning(
                        f"⚠️  Skipping Position {i}/{len(state.positions)}: {pos.symbol}; "
                        f"allowed value ${capped_position_value:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}"
                    )
                    continue
                if capped_position_value < requested_position_value:
                    logger.warning(
                        f"⚠️  Capping {pos.symbol} startup copy from "
                        f"${requested_position_value:,.2f} to ${capped_position_value:,.2f} "
                        f"(available-margin or MAX_POSITION_SIZE limit)"
                    )
                your_position_value = capped_position_value
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                margin_needed = your_position_value / your_leverage
                
                # Check minimum position size
                if your_position_value < MIN_POSITION_SIZE_USD:
                    logger.warning("")
                    logger.warning(f"⚠️  Skipping Position {i}/{len(state.positions)}: {pos.symbol}")
                    logger.warning(f"   Position value ${your_position_value:.2f} below Hyperliquid minimum ${MIN_POSITION_SIZE_USD:.2f}")
                    continue
                
                logger.info("")
                logger.info(f"📊 Copying Position {i}/{len(state.positions)}: {pos.symbol}")
                logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
                logger.info(f"   Target Value: ${target_position_value:,.2f}")
                logger.success(f"   → Your Size: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
                logger.success(f"   → Your Value: ${your_position_value:,.2f}")
                logger.success(f"   → Margin: ${margin_needed:,.2f}")
                
                # Execute the copy
                position_side = pos.side
                result = await executor.execute_market_order(
                    symbol=pos.symbol,
                    side=OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL,
                    size=your_size,
                    leverage=your_leverage
                )
                
                if result:
                    # Update simulated account
                    if settings.simulated_trading:
                        simulated_positions[pos.symbol] = {
                            'size': your_size if position_side == PositionSide.LONG else -your_size,
                            'entry_price': pos.entry_price,
                            'side': position_side.value.upper(),
                            'leverage': your_leverage,
                            'value': your_position_value,
                            'margin_used': margin_needed
                        }
                    
                    copied_count += 1
                    logger.success(f"   ✅ Position copied successfully!")
                else:
                    logger.error(f"   ❌ Failed to copy position")
                    
            except Exception as e:
                logger.error(f"   ❌ Error copying position {pos.symbol}: {e}")
        
        # Show final account state
        if settings.simulated_trading:
            total_margin_used = sum(p['margin_used'] for p in simulated_positions.values())
            logger.info("")
            logger.info("=" * 60)
            logger.success("✅ EXISTING POSITIONS COPIED!")
            logger.info("=" * 60)
            logger.success(f"💰 Simulated Account Update:")
            logger.success(f"   Total Positions Copied: {copied_count}/{len(state.positions)}")
            logger.success(f"   Total Margin Used: ${total_margin_used:,.2f}")
            logger.success(f"   Account Balance: ${simulated_balance:,.2f}")
            logger.success(f"   Available Balance: ${simulated_balance - total_margin_used:,.2f}")
            logger.info("=" * 60)
        
        # Update global counter
        trades_copied_count += copied_count
    
    # Initialize Telegram bot if configured
    if settings.telegram.bot_token and settings.telegram.chat_id:
        try:
            from telegram_bot import TelegramBot, NotificationService
        except ImportError as exc:
            raise RuntimeError(
                "Telegram is configured but optional dependencies are missing. "
                "Install requirements-telegram.txt or clear TELEGRAM_BOT_TOKEN "
                "and TELEGRAM_CHAT_ID."
            ) from exc

        logger.info("🤖 Initializing Telegram bot...")
        
        notifier = NotificationService(
            settings.telegram.bot_token,
            settings.telegram.chat_id
        )
        
        telegram_bot = TelegramBot(
            settings.telegram.bot_token,
            settings.telegram.chat_id
        )
        
        # Set up Telegram callbacks
        telegram_bot.get_status_callback = get_status
        telegram_bot.get_positions_callback = get_positions_formatted
        telegram_bot.get_orders_callback = get_orders
        telegram_bot.get_pnl_callback = get_pnl
        telegram_bot.get_leaderboard_callback = get_leaderboard
        telegram_bot.get_wallet_callback = get_wallet_report
        telegram_bot.on_pause_requested = handle_pause
        telegram_bot.on_resume_requested = handle_resume
        telegram_bot.on_stop_requested = handle_stop
        
        # Start Telegram bot
        await telegram_bot.start()
        
        # Start hourly reports task
        asyncio.create_task(send_hourly_reports())
        
        logger.info("✅ Telegram bot ready!")
    else:
        logger.warning("⚠️ Telegram bot not configured (add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env)")
    
    try:
        # Get initial state
        logger.info("")
        logger.info(f"📊 Fetching initial state...")
        state = await monitor.get_current_state()
        
        if state:
            logger.info("")
            logger.info(f"💼 Target Account:")
            logger.info(f"   Balance: ${state.balance:,.2f}")
            logger.info(f"   Equity: ${state.total_equity:,.2f}")
            logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
            logger.info(f"   Open Positions: {len(state.positions)}")
            
            if state.positions:
                logger.info("")
                logger.info(f" Current Positions:")
                for i, pos in enumerate(state.positions, 1):
                    logger.info(f"   {i}. {pos.symbol} {pos.side.value.upper()}: {pos.size} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
        
        # Copy existing open orders if configured
        if settings.copy_rules.copy_existing_orders and state and state.orders:
            logger.info("")
            logger.info(f"📋 Copying {len(state.orders)} existing orders...")
            for order in state.orders:
                try:
                    order_dict = {
                        'coin': order.symbol,
                        'oid': order.order_id,
                        '_target_oid': order.order_id,
                        'side': order.side,
                        'orderType': order.order_type,
                        'sz': str(order.size),
                        'limitPx': str(order.price),
                        '_startup_snapshot': True,
                    }
                    await on_new_order(order_dict)
                except Exception as e:
                    logger.error(f"Failed to copy existing order: {e}")
        
        logger.info("")
        logger.info(f"🔌 Starting monitoring...")
        logger.info("✅ Bot is now LIVE and monitoring for trades!")
        logger.info(f"   Copy Open Positions: {settings.copy_rules.copy_open_positions}")
        logger.info(f"   Copy Existing Orders: {settings.copy_rules.copy_existing_orders}")
        logger.info(f"   Mirror Pending Orders: {settings.copy_rules.mirror_pending_orders}")
        logger.info(f"   Cancel Mirrored Orders: {settings.copy_rules.cancel_mirrored_orders}")
        logger.info(f"   Auto Adjust Size: {settings.copy_rules.auto_adjust_size}")
        logger.info(f"   Max Open Trades: {'Unlimited' if settings.copy_rules.max_open_trades is None else settings.copy_rules.max_open_trades}")
        logger.info(f"   Max Open Orders: {'Unlimited' if settings.copy_rules.max_open_orders is None else settings.copy_rules.max_open_orders}")
        logger.info(f"   Max Account Equity: {'Unlimited' if settings.copy_rules.max_account_equity is None else f'${settings.copy_rules.max_account_equity:,.2f}'}")
        logger.info("Press Ctrl+C to stop\n")
        
        # Send startup notification
        if notifier:
            await notifier.send_startup_notification(
                target_wallet=settings.target_wallet,
                sizing_mode=settings.sizing.mode,
                ratio=f"1:{int(1/settings.sizing.portfolio_ratio)}",
                leverage_adjustment=settings.leverage.adjustment_ratio
            )
        
        # Start monitoring
        await monitor.start_monitoring()
        
    except KeyboardInterrupt:
        logger.info("")
        logger.info("⚠️ Shutdown signal received...")
    except Exception as e:
        logger.error
        logger.error(f"❌ Error: {e}")
        raise
    finally:
        logger.info("")
        logger.info("🛑 Stopping monitoring...")
        
        # Send shutdown notification
        if notifier:
            await notifier.send_shutdown_notification()
        
        # Stop components
        if monitor:
            await monitor.stop_monitoring()
        
        if telegram_bot:
            await telegram_bot.stop()
        
        logger.info("👋 Bot stopped gracefully")

if __name__ == "__main__":
    asyncio.run(main())
