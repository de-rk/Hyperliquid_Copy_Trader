"""Detect target-wallet fund flows from changes in account equity.

Why this exists
---------------
The copy ratio is derived from the target's account value. When the target
deposits or withdraws, that value jumps even though no position changed, and
the follower's sizing silently changes with it. The exchange does not push a
"deposit happened" event, so the only practical signal is a periodic snapshot.

A raw equity jump is NOT a deposit: with an unchanged position, a price move
changes unrealized PnL by the same amount. That is why the trigger here is the
*residual* after removing the unrealized-PnL change::

    net_flow = (equity_now - equity_before) - (upnl_now - upnl_before)

With sizes unchanged there were no fills, so realized PnL, fees and funding are
small; the residual is the cash that moved in or out. The exchange's public
``userNonFundingLedgerUpdates`` endpoint is then used to attach exact deposit /
withdrawal amounts to that estimate.

This module is deliberately pure IO-free logic: main.py does the polling and
sending, this module only decides. That keeps the detection testable without
mocking aiohttp or Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

SIZE_EPSILON = 1e-9


@dataclass(frozen=True)
class EquitySnapshot:
    """One point-in-time read of the target account.

    ``equity``, ``unrealized_pnl`` and ``positions`` must come from the same
    exchange read, otherwise the residual mixes in price moves from a
    different instant. ``positions`` holds (symbol, signed size) pairs; a
    signed size is used because a long that flips to a short is a big position
    change even when the absolute size stays similar.
    """

    wallet: str
    equity: float
    unrealized_pnl: float
    positions: tuple[tuple[str, float], ...]
    captured_at_ms: int

    @property
    def gross_size(self) -> float:
        return sum(abs(size) for _, size in self.positions)


@dataclass(frozen=True)
class EquityFlow:
    """A detected fund movement between two snapshots."""

    direction: str  # "in" | "out"
    net_flow: float  # signed estimate, USD
    equity_before: float
    equity_after: float
    equity_delta: float
    unrealized_pnl_delta: float
    flow_pct: float  # net_flow relative to the previous equity
    position_change_pct: float
    from_ms: int
    to_ms: int

    @property
    def amount(self) -> float:
        return abs(self.net_flow)

    @property
    def is_inflow(self) -> bool:
        return self.direction == "in"


def position_change_pct(previous: EquitySnapshot, current: EquitySnapshot) -> float:
    """Relative change in position sizes, ignoring price-driven notional moves.

    Notional is deliberately NOT used: a 10% price rise changes notional by
    10% with zero trading, which would look like a position change.
    """
    before = dict(previous.positions)
    after = dict(current.positions)
    symbols = set(before) | set(after)
    base = sum(abs(before.get(symbol, 0.0)) for symbol in symbols)
    diff = sum(
        abs(after.get(symbol, 0.0) - before.get(symbol, 0.0))
        for symbol in symbols
    )
    if base <= SIZE_EPSILON:
        # Flat -> flat is unchanged; flat -> anything is a full change.
        return 0.0 if diff <= SIZE_EPSILON else 100.0
    return diff / base * 100.0


def analyze_equity_flow(
    previous: EquitySnapshot,
    current: EquitySnapshot,
    *,
    min_usd: float,
    min_pct: float,
    max_position_change_pct: float,
) -> EquityFlow | None:
    """Return a flow when equity moved for reasons other than positions/PnL.

    Returns None for: a wallet switch (caller must reset the baseline), a bad
    read, a position change above tolerance, a pure mark-to-market move, or a
    residual below either threshold. Both the absolute and the relative
    threshold must be met so small accounts do not spam and large accounts do
    not miss a meaningful transfer.
    """
    if previous.wallet.lower() != current.wallet.lower():
        return None
    # A zero/negative equity is a failed or partial DEX aggregate, not a
    # withdrawal. Never compare against it and never emit an alert from it.
    if previous.equity <= 0 or current.equity <= 0:
        return None

    change = position_change_pct(previous, current)
    if change > max(0.0, max_position_change_pct):
        return None

    equity_delta = current.equity - previous.equity
    upnl_delta = current.unrealized_pnl - previous.unrealized_pnl
    net_flow = equity_delta - upnl_delta
    flow_pct = net_flow / max(abs(previous.equity), 1.0) * 100.0

    if abs(net_flow) < max(0.0, min_usd):
        return None
    if abs(flow_pct) < max(0.0, min_pct):
        return None

    return EquityFlow(
        direction="in" if net_flow > 0 else "out",
        net_flow=net_flow,
        equity_before=previous.equity,
        equity_after=current.equity,
        equity_delta=equity_delta,
        unrealized_pnl_delta=upnl_delta,
        flow_pct=flow_pct,
        position_change_pct=change,
        from_ms=previous.captured_at_ms,
        to_ms=current.captured_at_ms,
    )


def _as_float(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _same_wallet(value: Any, wallet: str) -> bool:
    return isinstance(value, str) and value.lower() == wallet.lower()


@dataclass(frozen=True)
class LedgerSummary:
    """Exact exchange cash movements inside the detection window."""

    ok: bool
    error: str = ""
    deposits: float = 0.0
    withdrawals: float = 0.0
    deposit_count: int = 0
    withdraw_count: int = 0
    other_net: float = 0.0
    other_counts: tuple[tuple[str, int], ...] = ()
    event_count: int = 0

    @property
    def chain_net(self) -> float:
        """Net external deposit/withdraw amount, already signed."""
        return self.deposits - self.withdrawals

    @property
    def matched(self) -> bool:
        """True when on-chain deposits/withdrawals were found."""
        return self.deposit_count > 0 or self.withdraw_count > 0


def summarize_ledger_updates(
    updates: Iterable[Mapping[str, Any]],
    wallet: str,
    *,
    start_ms: int,
    end_ms: int,
    ok: bool = True,
    error: str = "",
) -> LedgerSummary:
    """Sum public non-funding ledger events into deposit/withdraw totals.

    Hyperliquid returns entries shaped like
    ``{"time": ms, "delta": {"type": "deposit", "usdc": "100"}}``. Withdrawals
    split the amount and the fee into separate fields, so both are counted.
    Internal transfers are direction-sensitive: count them as an inflow when
    this wallet is the destination, an outflow when it is the source. Unknown
    shapes are counted but never assigned a sign, so a format change cannot
    produce a confidently wrong dollar amount.
    """
    deposits = withdrawals = other_net = 0.0
    deposit_count = withdraw_count = event_count = 0
    other_counts: dict[str, int] = {}

    for update in updates:
        if not isinstance(update, Mapping):
            continue
        timestamp = _as_float(update.get("time"))
        if timestamp is not None and not (start_ms <= timestamp <= end_ms):
            continue
        delta = update.get("delta")
        if not isinstance(delta, Mapping):
            continue
        event_count += 1
        kind = str(delta.get("type", "unknown"))
        usdc = _as_float(delta.get("usdc"))

        if kind == "deposit":
            amount = usdc if usdc is not None else 0.0
            deposits += abs(amount)
            deposit_count += 1
            continue
        if kind == "withdraw":
            fee = _as_float(delta.get("fee")) or 0.0
            withdrawals += abs((usdc if usdc is not None else 0.0) + fee)
            withdraw_count += 1
            continue

        other_counts[kind] = other_counts.get(kind, 0) + 1
        # Only USDC cash can be compared with account equity. ``usdcValue`` is
        # a token's mark-to-market estimate (a spotTransfer of 0.0025 USDE
        # reports 0.002503) and must NOT be summed into a dollar flow.
        amount = usdc
        token = str(delta.get("token", "")).upper()
        if amount is None and token in ("USDC", "USDH"):
            amount = _as_float(delta.get("amount"))
        if amount is None:
            continue
        if amount < 0:
            other_net += amount
        elif _same_wallet(delta.get("destination"), wallet):
            other_net += amount
        elif _same_wallet(delta.get("user"), wallet) or _same_wallet(
            delta.get("source"), wallet
        ):
            other_net -= amount
        elif str(delta.get("type", "")).lower() in ("rewardsclaim", "spogenesis"):
            other_net += amount
        # Otherwise the direction is unknown; do not invent a sign.

    return LedgerSummary(
        ok=ok,
        error=error,
        deposits=deposits,
        withdrawals=withdrawals,
        deposit_count=deposit_count,
        withdraw_count=withdraw_count,
        other_net=other_net,
        other_counts=tuple(sorted(other_counts.items())),
        event_count=event_count,
    )


def ledger_lines(summary: LedgerSummary) -> str:
    """Short human-readable breakdown for a Telegram message."""
    if not summary.ok:
        return f"流水查询失败：{summary.error or '未知错误'}（金额以推断值为准）"
    if summary.event_count == 0:
        return "窗口内无充提/划转流水（可能为净值口径差异或数据延迟）"
    parts = []
    if summary.deposit_count:
        parts.append(f"充值 +${summary.deposits:,.2f} × {summary.deposit_count} 笔")
    if summary.withdraw_count:
        parts.append(f"提现 −${summary.withdrawals:,.2f} × {summary.withdraw_count} 笔")
    if summary.other_counts:
        shown = list(summary.other_counts)[:4]
        detail = "、".join(f"{kind} × {count}" for kind, count in shown)
        if len(summary.other_counts) > len(shown):
            detail += f" 等 {len(summary.other_counts)} 类"
        parts.append(f"其他：{detail}")
        if abs(summary.other_net) > 0.005:
            parts.append(f"可判方向净额 {summary.other_net:+,.2f}")
    return "；".join(parts)
