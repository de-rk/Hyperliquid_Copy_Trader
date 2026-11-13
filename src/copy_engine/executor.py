"""Trade execution engine for Hyperliquid"""
import asyncio
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR
from eth_account import Account
import aiohttp

from utils.logger import logger
from hyperliquid.models import OrderType, OrderSide
from copy_engine.hl_signing import float_to_wire, get_timestamp_ms, sign_l1_action


# HIP-3 builder DEX assets occupy ``100000 + perp_dex_index * 10000 + index``.
# The default Perp DEX keeps the plain index, so its segment offset is 0 and the
# first builder DEX in ``perpDexs`` (index 1) starts at 110000.
HIP3_SEGMENT_BASE = 100000
HIP3_SEGMENT_WIDTH = 10000


@dataclass(frozen=True)
class PerpAsset:
    """A resolved Hyperliquid perp asset, HIP-3 builder DEXs included."""

    symbol: str
    dex: str
    asset_index: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool


def canonical_symbol(symbol: str) -> str:
    """Normalize a perp symbol without losing its builder DEX qualifier.

    Builder DEX symbols are ``{dex}:{coin}`` with a lowercase DEX prefix. Only
    the DEX part is normalized here; the ticker keeps its exchange casing so a
    mixed-case listing is never mangled.
    """
    raw = str(symbol or "").strip()
    if not raw:
        return raw
    if ":" in raw:
        dex, coin = raw.split(":", 1)
        return f"{dex.lower()}:{coin}"
    return raw


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
        # Asset registry covers the default Perp DEX and every HIP-3 builder
        # DEX. ``_assets`` keeps exact exchange symbols; ``_assets_ci`` allows a
        # lookup when an upstream caller has changed the case of a coin.
        self._assets: Dict[str, PerpAsset] = {}
        self._assets_ci: Dict[str, PerpAsset] = {}
        # Negative cache: symbol -> monotonic timestamp of the last failed
        # resolution, so an unsupported coin does not refresh the whole
        # universe on every fill it produces.
        self._unknown_assets: Dict[str, float] = {}
        self._metadata_loaded_at = 0.0
        # A newly listed HIP-3 asset must become resolvable without a restart:
        # unknown symbols trigger a forced refresh, and this TTL also bounds how
        # long a failed lookup is remembered.
        self._metadata_ttl_seconds = max(
            30, int(os.getenv("ASSET_METADATA_TTL_SECONDS", "300"))
        )
        self.last_error: Optional[str] = None
        # Hyperliquid leverage is symbol-level state. Repeating the same
        # update before every mirrored order burns exchange-action capacity.
        self._configured_leverages: Dict[str, int] = {}
        self._leverage_lock = asyncio.Lock()

        # Initialize signing account if we have credentials. A Hyperliquid API
        # (agent) wallet deliberately derives to a *different* address than the
        # account it trades for: the agent signs, the exchange attributes the
        # action to the approving account. Forcing the two addresses to match
        # here blocked exactly that setup, which is the recommended one.
        self.account = None
        self.agent_address: Optional[str] = None
        if self.private_key and not self.dry_run:
            try:
                self.account = Account.from_key(self.private_key)
                self.agent_address = self.account.address.lower()
                if self.agent_address != self.wallet_address:
                    logger.info(
                        f"🔑 API wallet {self.account.address} signs for "
                        f"account {self.wallet_address}; authorization is "
                        "verified against the exchange at startup"
                    )
                logger.info(f"✅ Executor initialized for wallet {self.wallet_address}")
            except Exception as e:
                logger.error(f"Failed to initialize signing account: {e}")
                raise
        elif not self.dry_run:
            raise ValueError("Cannot run in live mode without private key")
        else:
            logger.warning("⚠️ Running in DRY RUN mode - no real trades will be executed")

    async def _load_asset_metadata(self, force: bool = False) -> None:
        """Load the default and every HIP-3 builder DEX asset universe.

        Asset ids follow ``100000 + perp_dex_index * 10000 + index_in_meta``.
        ``perpDexs[0]`` is the default Perp DEX, so builder DEX ``k`` in
        ``perpDexs`` maps to the segment starting at
        ``HIP3_SEGMENT_BASE + k * HIP3_SEGMENT_WIDTH``. The universe is cached
        and refreshed on demand (unknown symbol) because a builder DEX can list
        assets without restarting this process.
        """
        if (
            not force
            and self._assets
            and time.monotonic() - self._metadata_loaded_at < self._metadata_ttl_seconds
        ):
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
            dex_offsets: Dict[str, int] = {"": 0}
            for index, dex in enumerate((perp_dexes or [])[1:], start=1):
                if dex and dex.get("name"):
                    dex_offsets[dex["name"]] = HIP3_SEGMENT_BASE + index * HIP3_SEGMENT_WIDTH

            assets: Dict[str, PerpAsset] = {}
            metas = await asyncio.gather(
                *(info({"type": "meta", "dex": dex}) for dex in dex_offsets)
            )
            for (dex, offset), metadata in zip(dex_offsets.items(), metas):
                for index, coin in enumerate((metadata or {}).get("universe", [])):
                    name = coin.get("name")
                    if not name:
                        continue
                    assets[name] = PerpAsset(
                        symbol=name,
                        dex=dex,
                        asset_index=offset + index,
                        sz_decimals=int(coin.get("szDecimals", 8)),
                        max_leverage=int(coin.get("maxLeverage") or 1),
                        only_isolated=bool(
                            coin.get("onlyIsolated")
                            or str(coin.get("marginMode", "")).lower() == "nocross"
                        ),
                    )

        self._assets = assets
        self._assets_ci = {symbol.lower(): asset for symbol, asset in assets.items()}
        self._metadata_loaded_at = time.monotonic()

    def _lookup_asset(self, symbol: str) -> Optional[PerpAsset]:
        """Resolve a symbol exactly, then with a normalized DEX prefix."""
        raw = str(symbol or "").strip()
        if not raw:
            return None
        asset = self._assets.get(raw)
        if asset is not None:
            return asset
        asset = self._assets.get(canonical_symbol(raw))
        if asset is not None:
            return asset
        return self._assets_ci.get(raw.lower())

    async def _resolve_asset(self, symbol: str) -> PerpAsset:
        """Return the asset, refreshing the universe only when needed."""
        # The hot path must not re-read the universe on a TTL: a known symbol
        # has to resolve from memory, or every refresh stalls an order.
        if not self._assets:
            await self._load_asset_metadata()
        asset = self._lookup_asset(symbol)
        if asset is None:
            # Covers a cold cache and a HIP-3 listing published after startup.
            # Negative-cache the miss so an unsupported coin cannot trigger a
            # full universe refresh on every fill it produces.
            key = str(symbol or "").strip().lower()
            missed_at = self._unknown_assets.get(key)
            if missed_at is not None and time.monotonic() - missed_at < self._metadata_ttl_seconds:
                raise ValueError(f"Unknown asset symbol: {symbol}")
            await self._load_asset_metadata(force=True)
            asset = self._lookup_asset(symbol)
            if asset is None:
                self._unknown_assets[key] = time.monotonic()
                raise ValueError(f"Unknown asset symbol: {symbol}")
            self._unknown_assets.pop(key, None)
        return asset

    async def _get_asset_info(self, symbol: str) -> Tuple[int, int]:
        asset = await self._resolve_asset(symbol)
        return asset.asset_index, asset.sz_decimals

    async def _get_asset_index(self, symbol: str) -> int:
        return (await self._resolve_asset(symbol)).asset_index

    async def get_asset(self, symbol: str) -> Optional[PerpAsset]:
        """Public, non-raising asset lookup for diagnostics and preflight."""
        try:
            return await self._resolve_asset(symbol)
        except ValueError:
            return None
        except Exception as exc:
            logger.error(f"Asset metadata unavailable for {symbol}: {exc}")
            return None

    async def get_max_leverage(self, symbol: str) -> Optional[int]:
        """Exchange-declared max leverage for a symbol, HIP-3 dexes included."""
        asset = await self.get_asset(symbol)
        return asset.max_leverage if asset else None

    async def _get_mid_price(self, symbol: str) -> float:
        asset = await self.get_asset(symbol)
        dex = asset.dex if asset else (symbol.split(":", 1)[0] if ":" in symbol else "")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.info_url,
                json={"type": "allMids", "dex": dex},
                headers={"Content-Type": "application/json"},
            ) as response:
                response.raise_for_status()
                mids = await response.json()
        key = asset.symbol if asset is not None else symbol
        value = mids.get(key) if isinstance(mids, dict) else None
        if value is None and isinstance(mids, dict):
            lowered = str(key).lower()
            value = next((px for name, px in mids.items() if name.lower() == lowered), None)
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
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

    @staticmethod
    def _wire_limit_price(
        price: Decimal,
        size_decimals: int,
        side: OrderSide,
    ) -> str:
        """Format a valid Hyperliquid limit price without worsening the limit.

        A merged order's weighted average often lands between valid price ticks.
        Hyperliquid permits at most five significant figures and
        ``6 - szDecimals`` decimal places for perp prices. Buy limits round
        down; sell limits round up, preserving the user's price boundary.
        """
        value = Decimal(str(price))
        if value <= 0:
            raise ValueError("Limit price must be positive")

        max_decimal_tick = Decimal(1).scaleb(-max(0, 6 - size_decimals))
        significant_tick = Decimal(1).scaleb(value.adjusted() - 4)
        tick = max(max_decimal_tick, significant_tick)
        rounding = ROUND_FLOOR if side == OrderSide.BUY else ROUND_CEILING
        return float_to_wire(value.quantize(tick, rounding=rounding))

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
            asset = await self._resolve_asset(symbol)
            # HIP-3 assets declare their own leverage ceiling, and some are
            # isolated-only. Sending a higher value or a cross request to an
            # isolated-only market is rejected and would block every order.
            requested = max(1, int(leverage))
            if requested > asset.max_leverage:
                logger.warning(
                    f"Leverage {requested}x exceeds {asset.symbol} max "
                    f"{asset.max_leverage}x; clamping"
                )
                requested = asset.max_leverage
            if asset.only_isolated and is_cross:
                logger.info(
                    f"{asset.symbol} is isolated-only on {asset.dex or 'default'} DEX; "
                    "using isolated margin"
                )
                is_cross = False
            leverage = requested
            asset_index = asset.asset_index
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
                            self.last_error = None
                            logger.success(f"✅ Updated leverage for {symbol} to {leverage}x")
                            return True
                        self.last_error = f"Hyperliquid rejected leverage update: {result}"
                        logger.error(f"Hyperliquid rejected leverage update: {result}")
                        return False
                    else:
                        error_text = await response.text()
                        self.last_error = f"HTTP {response.status} updating leverage: {error_text}"
                        logger.error(f"Failed to update leverage: {error_text}")
                        return False

        except Exception as e:
            self.last_error = f"Error updating leverage: {e}"
            logger.error(f"Error updating leverage: {e}")
            return False

    async def _ensure_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage once per symbol/value during this process lifetime."""
        if leverage <= 1:
            return True
        async with self._leverage_lock:
            if self._configured_leverages.get(symbol) == leverage:
                logger.debug(f"Leverage already configured for {symbol}: {leverage}x")
                return True
            if not await self._update_leverage(symbol, leverage):
                return False
            self._configured_leverages[symbol] = leverage
            return True

    async def prepare_leverage(self, symbol: str, leverage: int) -> bool:
        """Configure leverage before a unified-account capacity preflight."""
        if self.dry_run:
            return True
        async with self._leverage_lock:
            if self._configured_leverages.get(symbol) == leverage:
                return True
            if not await self._update_leverage(symbol, leverage):
                return False
            self._configured_leverages[symbol] = leverage
            return True

    async def preview_order(
        self,
        symbol: str,
        side: OrderSide,
        size: Decimal,
        price: Optional[Decimal] = None,
        reduce_only: bool = False,
        post_only: bool = False,
    ) -> Dict[str, Any]:
        """Build the exact exchange payload without signing or sending it.

        HIP-3 orders carry no DEX field: the builder DEX is encoded in the
        asset id, so a resolved ``xyz:SKHX`` order is structurally identical to
        a default-DEX order.
        """
        asset = await self._resolve_asset(symbol)
        if price is None:
            mid_price = await self._get_mid_price(symbol)
            slippage = self.max_slippage_pct / 100.0
            aggressive = mid_price * (1 + slippage if side == OrderSide.BUY else 1 - slippage)
            wire_price = self._wire_price(Decimal(str(aggressive)), asset.sz_decimals)
            tif = "Ioc"
        else:
            wire_price = self._wire_limit_price(price, asset.sz_decimals, side)
            tif = "Alo" if post_only else "Gtc"
        return {
            "type": "order",
            "orders": [{
                "a": asset.asset_index,
                "b": side == OrderSide.BUY,
                "p": wire_price,
                "s": self._wire_size(size, asset.sz_decimals),
                "r": reduce_only,
                "t": {"limit": {"tif": tif}},
            }],
            "grouping": "na",
        }

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
            if not await self._ensure_leverage(symbol, leverage):
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
                            self.last_error = None
                            logger.success(
                                f"✅ Market {side.value} order accepted: {symbol} "
                                f"size={size} leverage={leverage}x"
                            )
                            return order_id or "accepted"
                        self.last_error = f"Hyperliquid rejected market order: {result}"
                        logger.error(f"Hyperliquid rejected market order: {result}")
                        return None
                    else:
                        error_text = await response.text()
                        self.last_error = f"HTTP {response.status} executing market order: {error_text}"
                        logger.error(f"Failed to execute market order: {error_text}")
                        return None

        except Exception as e:
            self.last_error = f"Error executing market order: {e}"
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
            if not await self._ensure_leverage(symbol, leverage):
                logger.error(f"Cannot place {symbol} order because leverage update failed")
                return None

            asset_index, size_decimals = await self._get_asset_info(symbol)
            tif = "Alo" if post_only else "Gtc"
            wire_price = self._wire_limit_price(price, size_decimals, side)
            if Decimal(wire_price) != Decimal(str(price)):
                logger.info(
                    f"Normalized {symbol} limit price ${price} -> ${wire_price} "
                    f"for exchange tick size"
                )

            action = {
                "type": "order",
                "orders": [{
                    "a": asset_index,
                    "b": side == OrderSide.BUY,
                    "p": wire_price,
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
                            self.last_error = None
                            logger.success(
                                f"✅ Limit {side.value} order accepted: {symbol} "
                                f"size={size} price={price} leverage={leverage}x"
                            )
                            return order_id or "accepted"
                        self.last_error = f"Hyperliquid rejected limit order: {result}"
                        logger.error(f"Hyperliquid rejected limit order: {result}")
                        return None
                    else:
                        error_text = await response.text()
                        self.last_error = f"HTTP {response.status} placing limit order: {error_text}"
                        logger.error(f"Failed to place limit order: {error_text}")
                        return None

        except Exception as e:
            self.last_error = f"Error placing limit order: {e}"
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
                    "o": int(order_id) if str(order_id).isdigit() else order_id,
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
                        statuses = result.get("response", {}).get("data", {}).get("statuses", [])
                        rejected = any(
                            isinstance(status, dict) and "error" in status
                            for status in statuses
                        )
                        if result.get("status") == "ok" and not rejected:
                            self.last_error = None
                            logger.success(f"✅ Cancelled order {order_id} for {symbol}")
                            return True
                        self.last_error = f"Hyperliquid rejected cancel: {result}"
                        logger.error(self.last_error)
                        return False
                    else:
                        error_text = await response.text()
                        self.last_error = f"HTTP {response.status} cancelling order: {error_text}"
                        logger.error(f"Failed to cancel order: {error_text}")
                        return False

        except Exception as e:
            self.last_error = f"Error cancelling order: {e}"
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
