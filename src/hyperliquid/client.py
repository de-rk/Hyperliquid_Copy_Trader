import asyncio
import aiohttp
import json
import re
from time import monotonic
from typing import Optional, List, Dict, Any
from loguru import logger
from .models import Position, Order, UserState, PositionSide, OrderSide

class HyperliquidClient:
    """
    Client for interacting with Hyperliquid REST API
    """
    
    def __init__(
        self,
        api_url: str = "https://api.hyperliquid.xyz",
        leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
    ):
        self.api_url = api_url
        self.info_url = f"{api_url}/info"
        self.exchange_url = f"{api_url}/exchange"
        self.leaderboard_url = leaderboard_url
        # The first null entry is the default perp DEX. Additional HIP-3 DEXs
        # are discovered from the official perpDexs endpoint at runtime.
        self.dexs = [""]
        self.session: Optional[aiohttp.ClientSession] = None
        # A target order burst can request several account snapshots at once.
        # Keep Info calls bounded and share an immediately recent portfolio
        # response rather than amplifying a temporary API slowdown.
        self._request_semaphore = asyncio.Semaphore(4)
        self._portfolio_cache: Dict[str, tuple[float, Any]] = {}
        self._portfolio_locks: Dict[str, asyncio.Lock] = {}
        self._portfolio_cache_ttl_seconds = 5.0
        # Spot pair ids ("@142") are opaque; labels are resolved once per process.
        self._spot_pair_labels: Optional[Dict[str, str]] = None
        
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def close(self) -> None:
        """Close the reusable HTTP session owned by this client."""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
    
    async def _post(self, url: str, data: dict) -> Any:
        """Make one bounded Info request with retry for transient failures."""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()

        last_error: Optional[Exception] = None
        async with self._request_semaphore:
            for attempt in range(3):
                try:
                    async with self.session.post(
                        url,
                        json=data,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as response:
                        if response.status == 429 or response.status >= 500:
                            error_text = await response.text()
                            last_error = RuntimeError(
                                f"HTTP {response.status}: {error_text[:300]}"
                            )
                        else:
                            response.raise_for_status()
                            return await response.json()
                except aiohttp.ClientResponseError as exc:
                    # Request validation and authentication errors do not
                    # improve with retries.
                    if 400 <= exc.status < 500 and exc.status != 429:
                        logger.error(f"API request failed: {exc}")
                        raise
                    last_error = exc
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    last_error = exc

                if attempt < 2:
                    delay = 0.5 * (2 ** attempt)
                    logger.warning(
                        f"Info request failed ({last_error}); retrying in {delay:.1f}s "
                        f"({attempt + 1}/3): {data.get('type', 'unknown')}"
                    )
                    await asyncio.sleep(delay)

        logger.error(f"API request failed after retries: {last_error}")
        raise last_error or RuntimeError("API request failed without an error")

    async def _get_portfolio(self, address: str) -> Any:
        """Return a short-lived shared portfolio response for one wallet."""
        cached = self._portfolio_cache.get(address)
        now = monotonic()
        if cached and now - cached[0] < self._portfolio_cache_ttl_seconds:
            return cached[1]

        lock = self._portfolio_locks.setdefault(address, asyncio.Lock())
        async with lock:
            cached = self._portfolio_cache.get(address)
            now = monotonic()
            if cached and now - cached[0] < self._portfolio_cache_ttl_seconds:
                return cached[1]
            response = await self._post(
                self.info_url, {"type": "portfolio", "user": address}
            )
            self._portfolio_cache[address] = (monotonic(), response)
            return response
    
    async def get_user_state(
        self, address: str, dex: Optional[str] = None
    ) -> Optional[UserState]:
        """
        Get complete user state including positions and orders
        
        Args:
            address: Wallet address to query
            dex: A specific perp DEX. ``None`` returns the aggregate state.
            
        Returns:
            UserState object or None if failed
        """
        try:
            if dex is None:
                # Keep the list current as HIP-3 DEXs are added or removed.
                perp_dexes = await self._post(self.info_url, {"type": "perpDexs"})
                dexes = [""] + [
                    item["name"] for item in (perp_dexes or [])[1:]
                    if item and item.get("name")
                ]
                self.dexs = dexes
            else:
                # Builder DEX names are lowercase on the exchange; a string
                # comparison against the wrong case silently returns no state.
                dexes = [dex.lower() if dex else dex]
            # merge all dex responses to get complete user state across all dexs
            all_responses = None
            margin_totals = {
                "accountValue": 0.0,
                "totalMarginUsed": 0.0,
                "totalNtlPos": 0.0,
            }
            for current_dex in dexes:
                data = {
                    "type": "clearinghouseState",
                    "user": address,
                    "dex": current_dex
                }
                
                response = await self._post(self.info_url, data)
                
                if not isinstance(response, dict):
                    continue
                if all_responses is None:
                    all_responses = response
                else:
                    # Merge asset positions
                    if "assetPositions" in response:
                        if "assetPositions" not in all_responses:
                            all_responses["assetPositions"] = []
                        all_responses["assetPositions"].extend(response["assetPositions"])
                    
                    # Merge open orders
                    if "openOrders" in response:
                        if "openOrders" not in all_responses:
                            all_responses["openOrders"] = []
                        all_responses["openOrders"].extend(response["openOrders"])

                # Sum each DEX exactly once, including responses without positions.
                summary = response.get("marginSummary", {})
                for key in margin_totals:
                    margin_totals[key] += float(summary.get(key, 0) or 0)
                                
            
            # Parse positions
            positions = []
            if all_responses and "assetPositions" in all_responses:
                for pos_data in all_responses["assetPositions"]:
                    position = pos_data.get("position", {})
                    if position and position.get("szi") != "0":  # szi is the position size
                        size = float(position.get("szi", 0))
                        side = PositionSide.LONG if size > 0 else PositionSide.SHORT
                        
                        positions.append(Position(
                            symbol=position.get("coin", "not found"),
                            side=side,
                            size=abs(size),
                            entry_price=float(position.get("entryPx", 0)),
                            current_price=float(position.get("positionValue", 0)) / abs(size) if size != 0 else 0,
                            leverage=float(position.get("leverage", {}).get("value", 1)),
                            unrealized_pnl=float(position.get("unrealizedPnl", 0)),
                            liquidation_price=float(position.get("liquidationPx")) if position.get("liquidationPx") else None,
                            margin=float(position.get("marginUsed", 0))
                        ))
            
            # Parse orders
            orders = []
            if all_responses and "openOrders" in all_responses:
                for order_data in all_responses["openOrders"]:
                    order = order_data.get("order", {})
                    orders.append(Order(
                        order_id=str(order.get("oid", "")),
                        symbol=order.get("coin", ""),
                        side=OrderSide.BUY if order.get("side") == "B" else OrderSide.SELL,
                        order_type=order.get("orderType", "limit").lower(),
                        size=float(order.get("sz", 0)),
                        price=float(order.get("limitPx", 0)) if order.get("limitPx") else None,
                        filled_size=float(order.get("szFilled", 0)),
                        status="open",
                        trigger_price=float(order.get("triggerPx", 0)) if order.get("triggerPx") else None
                    ))
            
            # Parse account balance. A wallet with no perp state is still a
            # valid account; return a zeroed state instead of raising here.
            balance = margin_totals["accountValue"]
            margin_used = margin_totals["totalMarginUsed"]
            # ``totalNtlPos`` is total position notional, not PnL. Sum the
            # exchange-provided PnL from each open position instead.
            unrealized_pnl = sum(position.unrealized_pnl for position in positions)
            
            from datetime import datetime
            return UserState(
                address=address,
                positions=positions,
                orders=orders,
                balance=balance,
                margin_used=margin_used,
                unrealized_pnl=unrealized_pnl,
                timestamp=datetime.utcnow()
            )
            
        except Exception as e:
            logger.error(f"Failed to get user state for {address}: {e}")
            return None

    async def validate_wallet_address(self, address: str) -> bool:
        """Check address format and confirm it is queryable on Hyperliquid."""
        normalized = str(address or "").strip().lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", normalized):
            return False
        try:
            # A single default-DEX state query is enough to distinguish an
            # invalid/nonexistent account without scanning every HIP-3 DEX.
            return await self.get_user_state(normalized, dex="") is not None
        except Exception as exc:
            logger.warning(f"Unable to validate wallet {normalized}: {exc}")
            return False

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            if isinstance(value, str):
                value = value.strip().replace(",", "").replace("%", "")
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _history_points(cls, payload: Any) -> List[tuple[int, float]]:
        """Normalize portfolio history points from Hyperliquid API responses."""
        if isinstance(payload, dict):
            for key in ("accountValueHistory", "history", "values", "data"):
                if key in payload:
                    return cls._history_points(payload[key])
            payload = [payload]

        points: List[tuple[int, float]] = []
        if not isinstance(payload, list):
            return points

        for item in payload:
            timestamp = value = None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                timestamp, value = item[0], item[1]
            elif isinstance(item, dict):
                timestamp = item.get("time", item.get("timestamp", item.get("t")))
                value = item.get("value", item.get("accountValue", item.get("v")))
            timestamp_number = cls._as_float(timestamp)
            value_number = cls._as_float(value)
            if timestamp_number is not None and value_number is not None:
                points.append((int(timestamp_number), value_number))
        return sorted(points, key=lambda point: point[0])

    @classmethod
    def _portfolio_windows(cls, response: Any) -> Dict[str, Any]:
        """Extract day/week/month payloads across documented response shapes."""
        windows: Dict[str, Any] = {}
        aliases = {
            "day": "day", "24h": "day", "week": "week", "7d": "week",
            "month": "month", "30d": "month",
            "perpday": "perpDay", "perpweek": "perpWeek",
            "perpmonth": "perpMonth", "perpalltime": "perpAllTime",
        }

        if isinstance(response, dict):
            source = response.get("data", response)
            if isinstance(source, dict):
                for key, payload in source.items():
                    normalized = aliases.get(str(key).lower())
                    if normalized:
                        windows[normalized] = payload
        elif isinstance(response, list):
            for item in response:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    normalized = aliases.get(str(item[0]).lower())
                    if normalized:
                        windows[normalized] = item[1]
                elif isinstance(item, dict):
                    window = item.get("window", item.get("period", item.get("timeframe")))
                    normalized = aliases.get(str(window).lower()) if window is not None else None
                    if normalized:
                        windows[normalized] = item
        return windows

    async def get_portfolio_performance(self, address: str) -> Dict[str, Optional[Dict[str, float]]]:
        """Return account-value changes for Hyperliquid's day/week/month windows.

        These are net-value changes, so deposits and withdrawals during a
        window affect the result. That is preferable to presenting a made-up
        realized PnL when the exchange does not provide complete cash-flow data.
        """
        empty = {"24H": None, "7D": None, "30D": None}
        try:
            response = await self._get_portfolio(address)
            windows = self._portfolio_windows(response)
            periods = {"24H": "day", "7D": "week", "30D": "month"}
            result: Dict[str, Optional[Dict[str, float]]] = {}
            for label, window in periods.items():
                points = self._history_points(windows.get(window))
                if len(points) < 2 or points[0][1] == 0:
                    result[label] = None
                    continue
                start_value = points[0][1]
                end_value = points[-1][1]
                change = end_value - start_value
                result[label] = {
                    "change": change,
                    "change_pct": change / start_value * 100,
                    "start_value": start_value,
                    "end_value": end_value,
                }
            return result
        except Exception as e:
            logger.error(f"Failed to get portfolio performance for {address}: {e}")
            return empty

    async def get_portfolio_account_value(self, address: str) -> Optional[float]:
        """Return the latest all-account value from Hyperliquid portfolio history."""
        try:
            response = await self._get_portfolio(address)
            windows = self._portfolio_windows(response)
            # The general day/week/month windows represent total account value;
            # their latest samples should agree, so prefer day and fall back.
            for window in ("day", "week", "month"):
                points = self._history_points(windows.get(window))
                if points:
                    return points[-1][1]
            return None
        except Exception as e:
            logger.error(f"Failed to get portfolio account value for {address}: {e}")
            return None

    async def get_portfolio_unrealized_pnl(self, address: str) -> Optional[float]:
        """Return the latest all-account PnL value from portfolio history."""
        try:
            response = await self._get_portfolio(address)
            windows = self._portfolio_windows(response)
            for window in ("day", "week", "month"):
                payload = windows.get(window)
                points = self._history_points(
                    payload.get("pnlHistory") if isinstance(payload, dict) else None
                )
                if points:
                    return points[-1][1]
            return None
        except Exception as e:
            logger.error(f"Failed to get portfolio PnL for {address}: {e}")
            return None

    async def get_non_funding_ledger_updates(
        self, address: str, start_ms: int, end_ms: int
    ) -> List[Dict[str, Any]]:
        """Return public deposit/withdraw/transfer events in a time window.

        Unlike the other read helpers this one raises on failure: an empty
        window and a failed lookup mean opposite things when explaining an
        equity jump, so the caller must be able to tell them apart.
        """
        response = await self._post(
            self.info_url,
            {
                "type": "userNonFundingLedgerUpdates",
                "user": address,
                "startTime": int(start_ms),
                "endTime": int(end_ms),
            },
        )
        if not isinstance(response, list):
            raise RuntimeError("userNonFundingLedgerUpdates did not return a list")
        return [item for item in response if isinstance(item, dict)]

    async def get_raw_user_fills(self, address: str) -> List[Dict[str, Any]]:
        """Return raw public fills for internal monitoring and formatting."""
        try:
            response = await self._post(self.info_url, {"type": "userFills", "user": address})
        except Exception as e:
            logger.error(f"Failed to get user fills for {address}: {e}")
            return []
        return [fill for fill in response if isinstance(fill, dict)] if isinstance(response, list) else []

    async def get_user_fills(self, address: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the newest public fills for an address, newest first."""
        fills: List[Dict[str, Any]] = []
        for fill in await self.get_raw_user_fills(address):
            timestamp = self._as_float(fill.get("time", fill.get("timestamp", 0))) or 0
            price = self._as_float(fill.get("px", fill.get("price")))
            size = self._as_float(fill.get("sz", fill.get("size")))
            if price is None or size is None:
                continue
            raw_side = str(fill.get("side", "")).upper()
            side = "买入" if raw_side in {"B", "BUY"} else "卖出" if raw_side in {"A", "S", "SELL"} else raw_side
            fills.append({
                "symbol": str(fill.get("coin", fill.get("symbol", "未知币种"))),
                "direction": str(fill.get("dir", "")),
                "side": side,
                "price": price,
                "size": size,
                "closed_pnl": self._as_float(fill.get("closedPnl")),
                "fee": self._as_float(fill.get("fee")),
                "timestamp": int(timestamp),
            })
        fills.sort(key=lambda fill: fill["timestamp"], reverse=True)
        return fills[:max(1, min(limit, 30))]

    async def get_spot_pair_labels(self) -> Dict[str, str]:
        """Map spot pair ids ("@142") to readable labels ("UBTC/USDC").

        The exchange names spot pairs by index, so raw fills are unreadable.
        Names never change, so the map is cached for the process lifetime.
        """
        if self._spot_pair_labels is not None:
            return self._spot_pair_labels

        labels: Dict[str, str] = {}
        try:
            meta = await self._post(self.info_url, {"type": "spotMeta"})
            if isinstance(meta, dict):
                tokens = {
                    int(token["index"]): token.get("name", "?")
                    for token in meta.get("tokens", [])
                    if isinstance(token, dict) and token.get("index") is not None
                }
                for pair in meta.get("universe", []):
                    if not isinstance(pair, dict) or pair.get("index") is None:
                        continue
                    parts = [
                        tokens.get(int(token_index), "?")
                        for token_index in pair.get("tokens", [])
                    ]
                    if len(parts) == 2:
                        labels[f"@{pair['index']}"] = f"{parts[0]}/{parts[1]}"
        except Exception as exc:
            logger.warning(f"Failed to load spot pair labels: {exc}")

        self._spot_pair_labels = labels
        return labels

    @staticmethod
    def _leaderboard_rows(response: Any) -> List[Dict[str, Any]]:
        """Accept the known leaderboard response wrappers without inventing data."""
        if isinstance(response, list):
            return [row for row in response if isinstance(row, dict)]
        if not isinstance(response, dict):
            return []
        for key in ("leaderboardRows", "rows", "data", "leaderboard"):
            rows = response.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return []

    async def get_leaderboard(
        self,
        window: str,
        sort_by: str = "pnl",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Fetch and normalize the public Hyperliquid leaderboard."""
        if window not in {"day", "week", "month"}:
            raise ValueError(f"Unsupported leaderboard window: {window}")
        if sort_by not in {"pnl", "roi"}:
            raise ValueError(f"Unsupported leaderboard sort: {sort_by}")

        params = {"timeWindow": window, "sortBy": sort_by}
        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(self.leaderboard_url, params=params) as response:
                    response.raise_for_status()
                    payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Failed to get Hyperliquid leaderboard: {e}")
            return []

        def window_values(row: Dict[str, Any]) -> Dict[str, Any]:
            """Extract values from APIs that return all windows per row."""
            for key in ("windowPerformances", "performances", "windows"):
                values = row.get(key)
                if isinstance(values, dict):
                    selected = values.get(window) or values.get({"day": "24H", "week": "7D", "month": "30D"}[window])
                    if isinstance(selected, dict):
                        return selected
                elif isinstance(values, list):
                    for item in values:
                        if isinstance(item, (list, tuple)) and len(item) >= 2 and str(item[0]).lower() in {window, {"day": "24h", "week": "7d", "month": "30d"}[window]}:
                            if isinstance(item[1], dict):
                                return item[1]
                        elif isinstance(item, dict) and str(item.get("window", item.get("period", ""))).lower() in {window, {"day": "24h", "week": "7d", "month": "30d"}[window]}:
                            return item
            return row

        normalized: List[Dict[str, Any]] = []
        for index, row in enumerate(self._leaderboard_rows(payload), 1):
            values = window_values(row)
            pnl = self._as_float(values.get("pnl", values.get("pnlUsd", values.get("profit"))))
            roi = self._as_float(values.get("roi", values.get("returnOnEquity", values.get("return"))))
            address = row.get("ethAddress") or row.get("address") or row.get("user") or ""
            display_name = row.get("displayName") or row.get("name") or ""
            if pnl is None and roi is None:
                continue
            normalized.append({
                "rank": int(row.get("rank", index)),
                "address": str(address),
                "name": str(display_name),
                "pnl": pnl,
                "roi": roi,
            })

        key = "roi" if sort_by == "roi" else "pnl"
        normalized.sort(key=lambda row: row[key] if row[key] is not None else float("-inf"), reverse=True)
        return normalized[:max(1, min(limit, 200))]
    
    async def get_all_assets(self) -> List[Dict[str, Any]]:
        """Return every perp asset with its owning DEX and exchange asset id.

        ``allPerpMetas`` covers the default Perp DEX and every HIP-3 builder
        DEX. The DEX segment matters: dropping it makes a builder asset look
        like a default-DEX index and sends orders to the wrong market.
        """
        try:
            data = {"type": "allPerpMetas"}
            response = await self._post(self.info_url, data)
            all_assets = []
            for perp_dex_index, market in enumerate(response or []):
                if not isinstance(market, dict):
                    continue
                if perp_dex_index == 0:
                    offset = 0
                else:
                    offset = 100000 + perp_dex_index * 10000
                for index, asset in enumerate(market.get("universe", [])):
                    if not isinstance(asset, dict) or not asset.get("name"):
                        continue
                    symbol = asset["name"]
                    dex = symbol.split(":", 1)[0] if ":" in symbol else ""
                    all_assets.append({
                        "symbol": symbol,
                        "dex": dex,
                        "asset_index": offset + index,
                        "sz_decimals": int(asset.get("szDecimals", 8)),
                        "max_leverage": int(asset.get("maxLeverage") or 1),
                        "only_isolated": bool(
                            asset.get("onlyIsolated")
                            or str(asset.get("marginMode", "")).lower() == "nocross"
                        ),
                    })
            return all_assets
        except Exception as e:
            logger.error(f"Failed to get assets: {e}")
            return []

    async def get_spot_balances(self, address: str) -> List[Dict[str, Any]]:
        """Return Spot balances for diagnostics, sizing, and funding guidance."""
        try:
            response = await self._post(
                self.info_url,
                {"type": "spotClearinghouseState", "user": address},
            )
            return response.get("balances", []) if isinstance(response, dict) else []
        except Exception as e:
            logger.error(f"Failed to get Spot balances for {address}: {e}")
            return []

    async def get_available_to_trade_notional(
        self,
        address: str,
        symbol: str,
        side: str,
        target_leverage: Optional[float] = None,
    ) -> Optional[float]:
        """Return Hyperliquid's per-market available notional for one side.

        ``activeAssetData.availableToTrade`` is ordered buy then sell and is
        account-aware, including for unified accounts. The Info API receives
        the actual user address, never the API/agent signer address.
        """
        side_index = 0 if str(side).lower() in {"buy", "long", "b"} else 1
        try:
            # A wrong-cased coin is answered with an HTTP 500, not an empty
            # result, so each spelling needs its own error boundary.
            response = None
            for candidate in self._symbol_candidates(symbol):
                try:
                    attempt = await self._post(
                        self.info_url,
                        {"type": "activeAssetData", "user": address, "coin": candidate},
                    )
                except Exception as exc:
                    logger.debug(f"activeAssetData rejected {candidate}: {exc}")
                    continue
                if isinstance(attempt, dict) and attempt.get("availableToTrade"):
                    response = attempt
                    break
            values = response.get("availableToTrade") if isinstance(response, dict) else None
            if not isinstance(values, (list, tuple)) or len(values) <= side_index:
                logger.error(
                    f"activeAssetData did not include availableToTrade for {address} {symbol}"
                )
                return None
            available = self._as_float(values[side_index])
            if available is None:
                return None
            current_leverage = self._as_float(
                (response.get("leverage") or {}).get("value")
            )
            # If the account is currently set above the copier's intended
            # leverage, scale capacity down conservatively. A higher intended
            # leverage is applied before live unified-account preflight.
            if (
                target_leverage is not None
                and current_leverage is not None
                and current_leverage > target_leverage > 0
            ):
                available *= target_leverage / current_leverage
            return available
        except Exception as e:
            logger.error(
                f"Failed to get available trading notional for {address} {symbol}: {e}"
            )
            return None

    async def get_wallet_total_equity(self, address: str) -> Optional[float]:
        """Return mode-independent total account equity from portfolio history."""
        return await self.get_portfolio_account_value(address)
    
    @staticmethod
    def _symbol_candidates(symbol: str) -> List[str]:
        """Yield the spellings worth trying against a case-sensitive endpoint.

        Hyperliquid matches the full ``{dex}:{coin}`` string exactly: a
        lowercase DEX prefix is required, and an endpoint that receives
        ``XYZ:SKHX`` answers with ``null`` rather than an error.
        """
        raw = str(symbol or "").strip()
        if not raw:
            return []
        candidates = [raw]
        if ":" in raw:
            dex, coin = raw.split(":", 1)
            for value in (f"{dex.lower()}:{coin}", f"{dex.lower()}:{coin.upper()}"):
                if value not in candidates:
                    candidates.append(value)
        else:
            if raw.upper() not in candidates:
                candidates.append(raw.upper())
        return candidates

    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Return the mid price for a symbol from the DEX that lists it.

        Builder DEX symbols are ``{dex}:{coin}``, so one ``allMids`` request
        scoped to that DEX replaces the previous scan across every DEX — the
        scan cost roughly one round trip per DEX on every copied position.
        """
        try:
            dex = symbol.split(":", 1)[0].lower() if ":" in symbol else ""
            response = await self._post(
                self.info_url, {"type": "allMids", "dex": dex}
            )
            if isinstance(response, dict):
                for candidate in self._symbol_candidates(symbol):
                    value = response.get(candidate)
                    if value is not None:
                        return float(value)
                lowered = str(symbol).lower()
                match = next(
                    (px for name, px in response.items() if name.lower() == lowered),
                    None,
                )
                if match is not None:
                    return float(match)
            logger.error(f"Market price for {symbol} not found on dex '{dex or 'default'}'")
            return None
        except Exception as e:
            logger.error(f"Failed to get market price for {symbol}: {e}")
            return None
