"""Trade execution engine for Hyperliquid"""
import time
from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_DOWN
from eth_account import Account
import aiohttp

from utils.logger import logger
from hyperliquid.models import OrderType, OrderSide
from copy_engine.hl_signing import float_to_wire, get_timestamp_ms, sign_l1_action


class TradeExecutor:
    """Executes trades on Hyperliquid exchange"""

    def __init__(
        self,
        wallet_address: str,
        private_key: str,
        info_url: str = "https://api.hyperliquid.xyz/info",
        exchange_url: str = "https://api.hyperliquid.xyz/exchange",
        dry_run: bool = True,
        max_slippage_pct: float = 1.0,
    ):
        self.wallet_address = wallet_address.lower() if wallet_address else None
        self.private_key = private_key
        self.info_url = info_url
        self.exchange_url = exchange_url
        self.dry_run = dry_run
        self.max_slippage_pct = max(0.01, float(max_slippage_pct))
        self._coin_index_cache: Dict[str, int] = {}
        self._coin_size_decimals: Dict[str, int] = {}
        self._metadata_loaded = False

        # Initialize signing account if we have credentials
        self.account = None
        if self.private_key and not self.dry_run:
            try:
                self.account = Account.from_key(self.private_key)
                # Validate address matches
                if self.account.address.lower() != self.wallet_address:
                    raise ValueError(
                        f"Private key address {self.account.address} doesn't match "
                        f"configured address {self.wallet_address}"
                    )
                logger.info(f"✅ Executor initialized for wallet {self.wallet_address}")
            except Exception as e:
                logger.error(f"Failed to initialize signing account: {e}")
                raise
        elif not self.dry_run:
            raise ValueError("Cannot run in live mode without private key")
        else:
            logger.warning("⚠️ Running in DRY RUN mode - no real trades will be executed")

    async def _load_asset_metadata(self) -> None:
        """Load the official default and HIP-3 asset universes.

        Builder DEX assets use the SDK-defined offsets (110000, 120000, ...),
        not the zero-based index from the default ``meta`` response.
        """
        if self._metadata_loaded:
            return

        async with aiohttp.ClientSession() as session:
            async def info(payload: Dict[str, Any]) -> Any:
                async with session.post(
                    self.info_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    response.raise_for_status()
                    return await response.json()

            perp_dexes = await info({"type": "perpDexs"})
            dex_names = [""]
            dex_offsets = {"": 0}
            for index, dex in enumerate((perp_dexes or [])[1:]):
                if dex and dex.get("name"):
                    name = dex["name"]
                    dex_names.append(name)
                    dex_offsets[name] = 110000 + index * 10000

            for dex in dex_names:
                metadata = await info({"type": "meta", "dex": dex})
                offset = dex_offsets[dex]
                for index, coin in enumerate(metadata.get("universe", [])):
                    name = coin.get("name")
                    if not name:
                        continue
                    self._coin_index_cache[name] = offset + index
                    self._coin_size_decimals[name] = int(coin.get("szDecimals", 8))

        self._metadata_loaded = True

    async def _get_asset_info(self, symbol: str) -> tuple[int, int]:
        await self._load_asset_metadata()
        if symbol not in self._coin_index_cache:
            raise ValueError(f"Unknown asset symbol: {symbol}")
        return self._coin_index_cache[symbol], self._coin_size_decimals[symbol]

    async def _get_asset_index(self, symbol: str) -> int:
        return (await self._get_asset_info(symbol))[0]

    async def _get_mid_price(self, symbol: str) -> float:
        dex = symbol.split(":", 1)[0] if ":" in symbol else ""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "allMids", "dex": dex},
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                mids = await response.json()
        try:
            return float(mids[symbol])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"No mid price returned for {symbol}") from exc

    @staticmethod
    def _wire_size(size: Decimal, size_decimals: int) -> str:
        quantum = Decimal(1).scaleb(-size_decimals)
        normalized = Decimal(str(size)).quantize(quantum, rounding=ROUND_DOWN)
        if normalized <= 0:
            raise ValueError("Order size rounds to zero")
        return float_to_wire(normalized)

    @staticmethod
    def _wire_price(price: Decimal, size_decimals: int) -> str:
        # Hyperliquid's SDK uses at most 6 - szDecimals decimal places for
        # perp prices and at most five significant figures.
        decimals = max(0, 6 - size_decimals)
        rounded = round(float(price), decimals)
        rounded = float(f"{rounded:.5g}")
        return float_to_wire(rounded)

    def _sign_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Sign an action using the official Hyperliquid L1 scheme."""
        if not self.account:
            raise ValueError("Cannot sign actions without account")
        nonce = get_timestamp_ms()
        is_mainnet = self.info_url.startswith("https://api.hyperliquid.xyz/")
        signature = sign_l1_action(
            self.account,
            action,
            None,
            nonce,
            None,
            is_mainnet,
        )
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }

    @staticmethod
    def _extract_order_id(result: Dict[str, Any]) -> Optional[str]:
        if result.get("status") != "ok":
            return None
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        if not statuses:
            return None
        status = statuses[0]
        for key in ("resting", "filled"):
            value = status.get(key)
            if isinstance(value, dict) and value.get("oid") is not None:
                return str(value["oid"])
        return None

    @staticmethod
    def _result_has_error(result: Dict[str, Any]) -> bool:
        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
        return any(isinstance(status, dict) and "error" in status for status in statuses)

    async def _update_leverage(
        self,
        symbol: str,
        leverage: int,
        is_cross: bool = True
    ) -> bool:
        try:
            asset_index = await self._get_asset_index(symbol)
            action = {
                "type": "updateLeverage",
                "asset": asset_index,
                "isCross": is_cross,
                "leverage": leverage
            }

            signed_action = self._sign_action(action)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get("status") == "ok" and not self._result_has_error(result):
                            logger.success(f"✅ Updated leverage for {symbol} to {leverage}x")
                            return True
                        logger.error(f"Hyperliquid rejected leverage update: {result}")
                        return False
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to update leverage: {error_text}")
                        return False

        except Exception as e:
            logger.error(f"Error updating leverage: {e}")
            return False

    async def execute_market_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        leverage: int = 1,
        reduce_only: bool = False
    ) -> Optional[str]:
        if self.dry_run:
            return await self._simulate_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=OrderType.MARKET,
                leverage=leverage
            )

        try:
            if leverage > 1:
                if not await self._update_leverage(symbol, leverage):
                    logger.error(f"Cannot place {symbol} order because leverage update failed")
                    return None

            asset_index, size_decimals = await self._get_asset_info(symbol)
            mid_price = await self._get_mid_price(symbol)
            slippage = self.max_slippage_pct / 100.0
            aggressive_price = mid_price * (1 + slippage if side == OrderSide.BUY else 1 - slippage)
            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": side == OrderSide.BUY,
                    "p": self._wire_price(Decimal(str(aggressive_price)), size_decimals),
                    "s": self._wire_size(size, size_decimals),
                    "r": reduce_only,
                    "t": {"limit": {"tif": "Ioc"}}
                }],
                "grouping": "na"
            }

            signed_action = self._sign_action(action)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    # Hyperliquid can return HTTP 200 with a business-level
                    # rejection. Always retain the response body for diagnosis.
                    if response.status == 200:
                        result = await response.json()
                        order_id = self._extract_order_id(result)
                        if result.get("status") == "ok" and not self._result_has_error(result):
                            logger.success(
                                f"✅ Market {side.value} order accepted: {symbol} "
                                f"size={size} leverage={leverage}x"
                            )
                            return order_id or "accepted"
                        logger.error(f"Hyperliquid rejected market order: {result}")
                        return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to execute market order: {error_text}")
                        return None

        except Exception as e:
            logger.error(f"Error executing market order: {e}")
            return None

    async def execute_limit_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        price: Decimal,
        leverage: int = 1,
        reduce_only: bool = False,
        post_only: bool = False
    ) -> Optional[str]:
        if self.dry_run:
            return await self._simulate_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=OrderType.LIMIT,
                price=price,
                leverage=leverage
            )

        try:
            if leverage > 1:
                if not await self._update_leverage(symbol, leverage):
                    logger.error(f"Cannot place {symbol} order because leverage update failed")
                    return None

            asset_index, size_decimals = await self._get_asset_info(symbol)
            tif = "Alo" if post_only else "Gtc"

            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": side == OrderSide.BUY,
                    # Target limit prices are already exchange-valid; keep
                    # their precision as the official SDK does.
                    "p": float_to_wire(price),
                    "s": self._wire_size(size, size_decimals),
                    "r": reduce_only,
                    "t": {"limit": {"tif": tif}}
                }],
                "grouping": "na"
            }

            signed_action = self._sign_action(action)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        order_id = self._extract_order_id(result)
                        if result.get("status") == "ok" and not self._result_has_error(result):
                            logger.success(
                                f"✅ Limit {side.value} order accepted: {symbol} "
                                f"size={size} price={price} leverage={leverage}x"
                            )
                            return order_id or "accepted"
                        logger.error(f"Hyperliquid rejected limit order: {result}")
                        return None
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to place limit order: {error_text}")
                        return None

        except Exception as e:
            logger.error(f"Error placing limit order: {e}")
            return None

    async def close_position(
        self,
        symbol: str,
        size: Optional[Decimal] = None,
        side: Optional[OrderSide] = None
    ) -> Optional[str]:
        if self.dry_run:
            if size and side:
                logger.info(f"🔵 DRY RUN: Would close {side.value} {size} {symbol}")
            else:
                logger.info(f"🔵 DRY RUN: Would close position {symbol}")
            return f"dry_run_close_{symbol}_{int(time.time())}"

        if size is None or side is None:
            logger.warning(f"⚠️ Size and/or side not provided for {symbol}, using reduce_only market order")
            return await self.execute_market_order(
                symbol=symbol,
                side=OrderSide.SELL,
                size=Decimal("0.001"),
                reduce_only=True
            )

        return await self.execute_market_order(
            symbol=symbol,
            side=side,
            size=size,
            reduce_only=True
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        if self.dry_run:
            logger.info(f"🔵 DRY RUN: Would cancel order {order_id} for {symbol}")
            return True

        try:
            asset_index = await self._get_asset_index(symbol)
            action = {
                "type": "cancel",
                "cancels": [{
                    "a": asset_index,
                    "o": order_id
                }]
            }

            signed_action = self._sign_action(action)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        logger.success(f"✅ Cancelled order {order_id} for {symbol}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to cancel order: {error_text}")
                        return False

        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        if self.dry_run:
            logger.info(f"🔵 DRY RUN: Would cancel all orders{f' for {symbol}' if symbol else ''}")
            return 0

        try:
            asset_index = await self._get_asset_index(symbol) if symbol else None
            action = {
                "type": "cancelByCloid",
                "cancels": [{
                    "asset": asset_index,
                    "cloid": None
                }]
            }

            signed_action = self._sign_action(action)

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.exchange_url,
                    json=signed_action,
                    headers={"Content-Type": "application/json"}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        count = len(result.get("response", {}).get("data", {}).get("statuses", []))
                        logger.success(f"✅ Cancelled {count} orders{f' for {symbol}' if symbol else ''}")
                        return count
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to cancel all orders: {error_text}")
                        return 0

        except Exception as e:
            logger.error(f"Error cancelling all orders: {e}")
            return 0

    async def _simulate_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        order_type: OrderType,
        price: Optional[Decimal] = None,
        leverage: int = 1
    ) -> str:
        order_id = f"sim_{symbol}_{int(time.time())}"

        if order_type == OrderType.MARKET:
            logger.info(
                f"🔵 DRY RUN: Would execute MARKET {side.value} {symbol} "
                f"size={size} leverage={leverage}x → Order ID: {order_id}"
            )
        else:
            logger.info(
                f"🔵 DRY RUN: Would place LIMIT {side.value} {symbol} "
                f"size={size} price={price} leverage={leverage}x → Order ID: {order_id}"
            )

        return order_id
