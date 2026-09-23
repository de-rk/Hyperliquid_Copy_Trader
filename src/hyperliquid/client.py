import asyncio
import aiohttp
import json
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
    
    async def _post(self, url: str, data: dict) -> dict:
        """Make POST request to API"""
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession()
            
        try:
            async with self.session.post(url, json=data) as response:
                response.raise_for_status()
                return await response.json()
        except aiohttp.ClientError as e:
            logger.error(f"API request failed: {e}")
            raise
    
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
                dexes = [dex]
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
            response = await self._post(self.info_url, {"type": "portfolio", "user": address})
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
            response = await self._post(self.info_url, {"type": "portfolio", "user": address})
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
            response = await self._post(self.info_url, {"type": "portfolio", "user": address})
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
        return fills[:max(1, min(limit, 20))]

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
        """Get list of all available trading assets"""
        try:
            data = {"type": "allPerpMetas"} # This endpoint returns metadata for all perpetual markets, including the different dex asset universe
            response = await self._post(self.info_url, data)
            all_assets = []
            for market in response:
                universe = market.get("universe", [])
                for asset in universe:
                    all_assets.append({
                        "symbol": asset
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

    async def get_wallet_total_equity(self, address: str) -> Optional[float]:
        """Return aggregate Perps equity plus Spot USDC collateral."""
        state = await self.get_user_state(address)
        if state is None:
            return None
        try:
            response = await self._post(
                self.info_url,
                {"type": "spotClearinghouseState", "user": address},
            )
        except Exception:
            logger.exception(f"Unable to read Spot collateral for {address}; sizing must not fall back to Perps-only equity")
            return None
        if not isinstance(response, dict) or not isinstance(response.get("balances"), list):
            logger.error(f"Spot collateral response is invalid for {address}; refusing Perps-only sizing")
            return None
        spot_usdc = sum(
            float(item.get("total", 0) or 0)
            for item in response["balances"]
            if str(item.get("coin", "")).upper() == "USDC"
        )
        return state.balance + spot_usdc
    
    async def get_market_price(self, symbol: str) -> Optional[float]:
        """Get current market price for a symbol"""
        try:
            if ":" in symbol and self.dexs == [""]:
                perp_dexes = await self._post(self.info_url, {"type": "perpDexs"})
                self.dexs = [""] + [
                    dex["name"] for dex in (perp_dexes or [])[1:]
                    if dex and dex.get("name")
                ]
            # The "allMids" endpoint returns the mid price for all symbols across all dexs, so we can just query it once and extract the price for the symbol we want
            find_symbol = False
            for dex in self.dexs:
                data = {
                    "type": "allMids",
                    "dex": dex
                }
                response = await self._post(self.info_url, data)
                if not response:
                    continue
                # Response is a dict with symbol: price
                if isinstance(response, dict) and symbol in response:
                    find_symbol = True
                    return float(response[symbol])
        
            if not find_symbol:
                logger.error(f"Market price for {symbol} not found")
                return None
            
        except Exception as e:
            logger.error(f"Failed to get market price for {symbol}: {e}")
            return None
