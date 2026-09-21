import asyncio
from typing import Callable, Optional, List
from loguru import logger
from hyperliquid.client import HyperliquidClient
from hyperliquid.websocket import HyperliquidWebSocket
from hyperliquid.models import Position, Order, UserState, WebSocketUpdate


class WalletMonitor:
    """
    Monitor a target wallet for trading activity
    """
    
    def __init__(
        self,
        target_address: str,
        api_url: str = "https://api.hyperliquid.xyz",
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
        fill_polling_enabled: bool = False,
        fill_poll_interval_seconds: int = 15,
    ):
        self.target_address = target_address
        self.client = HyperliquidClient(api_url)
        self.ws = HyperliquidWebSocket(ws_url)
        
        # Current state tracking
        self.current_state: Optional[UserState] = None
        self.last_positions: List[Position] = []
        self.last_orders: List[Order] = []
        self.is_monitoring = False
        self.observed_fill_ids: set[str] = set()
        self.fill_poll_task: Optional[asyncio.Task] = None
        # Hyperliquid can split one order into many fills. Keep fills for the
        # same order together briefly so the copier submits one meaningful
        # order instead of many sub-minimum orders.
        self.pending_fill_batches: dict[str, list[dict]] = {}
        self.pending_fill_tasks: dict[str, asyncio.Task] = {}
        self.fill_batch_window_seconds = 0.5
        self.fill_polling_enabled = fill_polling_enabled
        self.fill_poll_interval = max(15, fill_poll_interval_seconds)
        
        # Callbacks
        self.on_new_position: Optional[Callable] = None
        self.on_position_update: Optional[Callable] = None
        self.on_position_close: Optional[Callable] = None
        self.on_new_order: Optional[Callable] = None
        self.on_order_fill: Optional[Callable] = None
        self.on_order_cancel: Optional[Callable] = None
        
        logger.info(f"Wallet Monitor initialized for {target_address}")
    
    async def get_current_state(self) -> Optional[UserState]:
        """Fetch current state of target wallet"""
        self.current_state = await self.client.get_user_state(self.target_address)

        if self.current_state:
            self.last_positions = self.current_state.positions.copy()
            self.last_orders = self.current_state.orders.copy()

        return self.current_state
    
    async def start_monitoring(self):
        """Start monitoring the target wallet"""
        logger.info(f"Starting monitoring for {self.target_address}")
        self.is_monitoring = True
        
        # Establish the historical fill baseline before taking the initial
        # state snapshot. New fills after this point are handled by polling
        # even if the WebSocket subscription is still connecting.
        await self._seed_fill_baseline()
        await self.get_current_state()
        if self.fill_polling_enabled:
            self.fill_poll_task = asyncio.create_task(self._poll_fills())
            logger.info(
                f"Fill polling fallback enabled ({self.fill_poll_interval}s interval)"
            )
        else:
            logger.info("Fill polling fallback disabled; using WebSocket fills only")
        
        # Connect WebSocket
        await self.ws.connect()
        
        # Subscribe to user updates
        await self.ws.subscribe_user_events(self.target_address, self._handle_user_event)
        
        # Subscribe to order updates
        await self.ws.subscribe_order_updates(self.target_address, self._handle_order_update)
        
        # Start listening
        await self.ws.listen()
    
    async def stop_monitoring(self):
        """Stop monitoring"""
        logger.info("Stopping wallet monitoring")
        self.is_monitoring = False
        if self.fill_poll_task and not self.fill_poll_task.done():
            self.fill_poll_task.cancel()
            try:
                await self.fill_poll_task
            except asyncio.CancelledError:
                pass
        for task in self.pending_fill_tasks.values():
            task.cancel()
        self.pending_fill_tasks.clear()
        self.pending_fill_batches.clear()
        await self.ws.stop()
        await self.client.close()

    @staticmethod
    def _fill_id(fill: dict) -> str:
        trade_id = fill.get("tid")
        if trade_id is not None:
            return str(trade_id)
        return ":".join(str(fill.get(key, "")) for key in ("coin", "time", "oid", "sz", "px"))

    async def _seed_fill_baseline(self) -> None:
        """Remember old fills so startup never creates surprise catch-up orders."""
        fills = await self.client.get_raw_user_fills(self.target_address)
        self.observed_fill_ids.update(self._fill_id(fill) for fill in fills)
        logger.info(
            f"Fill baseline seeded with {len(self.observed_fill_ids)} historical fills; "
            "only fills after monitoring starts are eligible for copying"
        )

    async def _poll_fills(self) -> None:
        """Recover new fills that may be missed while the WebSocket reconnects."""
        while self.is_monitoring:
            try:
                await asyncio.sleep(self.fill_poll_interval)
                fills = await self.client.get_raw_user_fills(self.target_address)
                await self._handle_fills(fills, source="poll")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error polling target fills: {e}")
    
    async def _handle_user_event(self, update: WebSocketUpdate):
        """Handle WebSocket updates from target wallet"""
        logger.info(f"🔔 WebSocket Update Received: {update.channel}")
        
        try:
            if "data" not in update.data:
                logger.warning(f"⚠️ Update has no 'data' field: {update.data}")
                return
            
            data = update.data["data"]
            logger.info(f"📦 Update data keys: {list(data.keys())}")
            
            # Handle fills (completed trades)
            if "fills" in data:
                logger.success(f"💥 FILLS DETECTED: {len(data['fills'])} fills")
                await self._handle_fills(data["fills"], source="websocket")
            
            # Handle position updates
            if "positions" in data:
                logger.success(f"📊 POSITIONS UPDATE: {len(data['positions'])} positions")
                await self._handle_positions(data["positions"])
            
            # Handle order updates
            if "orders" in data:
                logger.success(f"📋 ORDERS UPDATE: {len(data['orders'])} orders")
                await self._handle_orders(data["orders"])
                
        except Exception as e:
            logger.error(f"Error handling update: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    async def _handle_fills(self, fills: List[dict], source: str = "websocket"):
        """Handle trade fills"""
        # Refresh positions before processing fills to ensure we have up-to-date state
        logger.debug("🔄 Refreshing position state before processing fills...")
        await self.get_current_state()
        
        new_fills = []
        for fill in fills:
            fill_id = self._fill_id(fill)
            if fill_id in self.observed_fill_ids:
                continue
            self.observed_fill_ids.add(fill_id)
            new_fills.append(fill)

        # Polling can return several fills from one reduce order. Process them
        # chronologically and annotate each with its own post-fill target size,
        # rather than using the final position size for every partial fill.
        new_fills.sort(key=lambda fill: int(fill.get("time", 0)))
        pending_close_sizes = {}
        for fill in new_fills:
            direction = str(fill.get("dir", ""))
            if "Close" in direction or "Reduce" in direction:
                symbol = str(fill.get("coin", "")).upper()
                pending_close_sizes[symbol] = pending_close_sizes.get(symbol, 0.0) + abs(
                    float(fill.get("sz", 0))
                )

        final_position_sizes = {
            position.symbol.upper(): position.size for position in self.current_state.positions
        } if self.current_state else {}

        for fill in new_fills:
            # Extract symbol from fill data
            symbol = fill.get("coin", "").upper()
            direction = str(fill.get("dir", ""))
            if "Close" in direction or "Reduce" in direction:
                fill_size = abs(float(fill.get("sz", 0)))
                pending_size = pending_close_sizes.get(symbol, 0.0)
                fill["_target_remaining_size_after_fill"] = (
                    final_position_sizes.get(symbol, 0.0) + max(0.0, pending_size - fill_size)
                )
                pending_close_sizes[symbol] = max(0.0, pending_size - fill_size)
            
            # Check if asset is blocked
            from config.settings import settings
            if symbol in settings.copy_rules.blocked_assets:
                logger.warning(f"⛔ BLOCKED ASSET - Ignoring fill for {symbol} (in blocked list)")
                continue
            
            await self._queue_fill_for_copy(fill, source)

    @staticmethod
    def _fill_batch_key(fill: dict) -> str:
        """Use the exchange order id when available; otherwise don't merge."""
        order_id = fill.get("oid")
        if order_id is not None and str(order_id):
            return f"oid:{order_id}"
        return f"fill:{WalletMonitor._fill_id(fill)}"

    async def _queue_fill_for_copy(self, fill: dict, source: str) -> None:
        """Debounce fills from one target order before invoking the copier."""
        key = self._fill_batch_key(fill)
        self.pending_fill_batches.setdefault(key, []).append(fill)

        previous = self.pending_fill_tasks.get(key)
        if previous and not previous.done():
            previous.cancel()
        self.pending_fill_tasks[key] = asyncio.create_task(
            self._flush_fill_batch(key, source)
        )

    async def _flush_fill_batch(self, key: str, source: str) -> None:
        try:
            await asyncio.sleep(self.fill_batch_window_seconds)
        except asyncio.CancelledError:
            return

        fills = self.pending_fill_batches.pop(key, [])
        self.pending_fill_tasks.pop(key, None)
        if not fills or not self.on_order_fill:
            return

        fills.sort(key=lambda fill: int(fill.get("time", 0)))
        if len(fills) == 1:
            merged = fills[0]
        else:
            # All fills in this batch share an order id. Use a size-weighted
            # execution price and retain the newest post-fill target size.
            total_size = sum(abs(float(fill.get("sz", 0))) for fill in fills)
            if total_size <= 0:
                return
            weighted_price = sum(
                abs(float(fill.get("sz", 0))) * float(fill.get("px", 0))
                for fill in fills
            ) / total_size
            merged = dict(fills[-1])
            merged["sz"] = total_size
            merged["px"] = weighted_price
            merged["_aggregated_fill_count"] = len(fills)
            merged["_aggregated_fill_ids"] = [self._fill_id(fill) for fill in fills]
            logger.info(
                f"📦 Aggregated {len(fills)} fills for target order "
                f"{merged.get('oid', key)}: size={total_size:.8f}"
            )

        logger.success(f"🎯 FILL DETECTED ({source}): {merged}")
        try:
            if asyncio.iscoroutinefunction(self.on_order_fill):
                await self.on_order_fill(merged)
            else:
                self.on_order_fill(merged)
        except Exception as e:
            logger.error(f"Error in fill callback: {e}")
    
    async def _handle_positions(self, positions: List[dict]):
        """Handle position updates"""
        logger.info(f"📍 Position update received: {len(positions)} positions")
        
        from config.settings import settings
        
        for pos_data in positions:
            # Parse position data
            symbol = pos_data.get("coin", "")
            size = float(pos_data.get("szi", 0))

            # Check if asset is blocked
            if symbol.upper() in settings.copy_rules.blocked_assets:
                logger.debug(f"⛔ Ignoring position update for blocked asset: {symbol}")
                continue
            
            # Check if this is a new position
            existing = next((p for p in self.last_positions if p.symbol == symbol), None)
            
            if not existing and size != 0:
                # NEW POSITION!
                logger.success(f"🆕 NEW POSITION DETECTED: {symbol}")
                
                if self.on_new_position:
                    try:
                        if asyncio.iscoroutinefunction(self.on_new_position):
                            await self.on_new_position(pos_data)
                        else:
                            self.on_new_position(pos_data)
                    except Exception as e:
                        logger.error(f"Error in new position callback: {e}")
            
            elif existing and size == 0:
                # POSITION CLOSED
                logger.info(f"❌ POSITION CLOSED: {symbol}")
                
                if self.on_position_close:
                    try:
                        if asyncio.iscoroutinefunction(self.on_position_close):
                            await self.on_position_close(pos_data)
                        else:
                            self.on_position_close(pos_data)
                    except Exception as e:
                        logger.error(f"Error in position close callback: {e}")
            
            elif existing and abs(size) != abs(existing.size):
                # POSITION SIZE CHANGED
                logger.info(f"📊 POSITION UPDATED: {symbol} ({existing.size} -> {size})")
                
                if self.on_position_update:
                    try:
                        if asyncio.iscoroutinefunction(self.on_position_update):
                            await self.on_position_update(pos_data)
                        else:
                            self.on_position_update(pos_data)
                    except Exception as e:
                        logger.error(f"Error in position update callback: {e}")
        
        # Update state
        await self.get_current_state()
    
    async def _handle_orders(self, orders: List[dict]):
        """Handle order updates"""
        logger.info(f"📝 Order update received: {len(orders)} orders")
        
        for order_data in orders:
            order_id = str(order_data.get("oid", ""))
            symbol = order_data.get("coin", "")
            
            # Check if new order
            existing = next((o for o in self.last_orders if o.order_id == order_id), None)
            
            if not existing:
                logger.success(f"📋 NEW ORDER: {symbol} - ID: {order_id}")
                
                if self.on_new_order:
                    try:
                        if asyncio.iscoroutinefunction(self.on_new_order):
                            await self.on_new_order(order_data)
                        else:
                            self.on_new_order(order_data)
                    except Exception as e:
                        logger.error(f"Error in new order callback: {e}")
        
        # Update state
        await self.get_current_state()
        
    async def _handle_order_update(self, update: WebSocketUpdate):
        """Handle order updates from WebSocket"""
        logger.info(f"🔔 Order Update Received: {update.channel}")
        
        try:
            if "data" not in update.data:
                logger.warning(f"⚠️ Update has no 'data' field: {update.data}")
                return
            
            data = update.data["data"]
            logger.info(f"📦 Order update data keys: {list(data.keys())}")
            
            if "orders" in data:
                logger.success(f"📋 ORDERS UPDATE: {len(data['orders'])} orders")
                await self._handle_orders(data["orders"])
                
        except Exception as e:
            logger.error(f"Error handling order update: {e}")
            import traceback
            logger.error(traceback.format_exc())
