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
# Floor for ledger-confirmed alerts. The configurable thresholds exist to keep
# noise out of the *inferred* residual; a real deposit or withdrawal is not
# noise, so it is only filtered for dust below this amount.
LEDGER_ALERT_MIN_USD = 1.0
# Hyperliquid's own spot-USDC system account: it holds the spot USDC book
# (tens of billions), and no ordinary wallet ever pays into it. Verified against
# the target's portfolio curve: each ``send`` here lowers total account value by
# exactly its size (six samples over four months, exact to the cent), so it is
# reported as a real outflow rather than excused as an own-book move. The label
# matters because "转出至 0x2000…0000" tells a human it is an exchange-side
# move, not a payment to another trader.
SYSTEM_ADDRESSES = {
    "0x2000000000000000000000000000000000000000": "系统地址",
}


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


def _peer_of(delta: Mapping[str, Any], wallet: str) -> str:
    """The other side of a peer transfer, normalised to lowercase."""
    for key in ("user", "source", "destination"):
        value = delta.get(key)
        if isinstance(value, str) and value and not _same_wallet(value, wallet):
            return value.lower()
    return ""


def _track_peer(peers: dict[str, list[float]], address: str, signed_net: float) -> None:
    if not address:
        return
    slot = peers.setdefault(address, [0.0, 0])
    slot[0] += signed_net
    slot[1] += 1


def short_address(address: str, *, head: int = 6, tail: int = 4) -> str:
    """0x62f87be0…75e7ac style shortening for chat messages."""
    if not address:
        return "未知"
    if len(address) <= head + tail + 1:
        return address
    return f"{address[:head]}…{address[-tail:]}"


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
    # Gross legs of the peer transfers, so a window that both took money in and
    # paid money out can be shown as two numbers instead of one net figure that
    # hides a six-figure movement on each side.
    send_in: float = 0.0
    send_out: float = 0.0
    # Transfers between this wallet's own spot and perp books. They change the
    # perp account value (which is what the snapshot compares) without moving
    # one cent in or out of the account, so they must never read as a deposit.
    internal_net: float = 0.0
    internal_count: int = 0
    other_counts: tuple[tuple[str, int], ...] = ()
    event_count: int = 0
    # Who the external transfers went to or came from, largest first:
    # ``(address, signed_net, count)``. A bare total says money moved; the
    # counterparty is what tells a funding wallet apart from a real exit.
    peer_flows: tuple[tuple[str, float, int], ...] = ()

    @property
    def chain_net(self) -> float:
        """Net external deposit/withdraw amount, already signed."""
        return self.deposits - self.withdrawals

    @property
    def matched(self) -> bool:
        """True when on-chain deposits/withdrawals were found."""
        return self.deposit_count > 0 or self.withdraw_count > 0

    def explains_internal(self, net_flow: float, *, tolerance: float = 5.0) -> bool:
        """True when own-book transfers alone account for the equity jump.

        Requires that no real deposit/withdrawal happened in the window: an
        external transfer plus an internal move could otherwise be excused by
        their sum by coincidence.
        """
        if self.matched:
            return False
        if self.internal_count == 0:
            return False
        slack = max(tolerance, abs(net_flow) * 0.1)
        return abs(net_flow - self.internal_net) <= slack


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
    this wallet is the destination, an outflow when it is the source. A
    transfer whose source AND destination are this same wallet is a spot/perp
    book move, not cash flow, and is reported through ``internal_net`` only.
    Unknown shapes are counted but never assigned a sign, so a format change
    cannot produce a confidently wrong dollar amount.
    """
    deposits = withdrawals = other_net = send_in = send_out = 0.0
    internal_net = 0.0
    deposit_count = withdraw_count = event_count = internal_count = 0
    other_counts: dict[str, int] = {}
    peers: dict[str, list[float]] = {}  # address -> [signed net, count]

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
        # Same wallet on both sides: the money only moved between this
        # account's spot and perp books (``send`` reports one row per leg).
        # That changes the perp account value without any cash entering or
        # leaving, so it must never be summed into ``other_net``. The signed
        # perp move is tracked separately so a pure internal transfer can be
        # told apart from a real deposit.
        if _same_wallet(delta.get("user"), wallet) and _same_wallet(
            delta.get("destination"), wallet
        ):
            internal_count += 1
            source_dex = str(delta.get("sourceDex", "")).lower()
            dest_dex = str(delta.get("destinationDex", "")).lower()
            if source_dex == "spot" and dest_dex != "spot":
                internal_net += abs(amount)
            elif dest_dex == "spot" and source_dex != "spot":
                internal_net -= abs(amount)
            continue
        if amount < 0:
            other_net += amount
            send_out += abs(amount)
            _track_peer(peers, _peer_of(delta, wallet), amount)
        elif _same_wallet(delta.get("destination"), wallet):
            other_net += amount
            send_in += amount
            _track_peer(peers, _peer_of(delta, wallet), amount)
        elif _same_wallet(delta.get("user"), wallet) or _same_wallet(
            delta.get("source"), wallet
        ):
            other_net -= amount
            send_out += amount
            _track_peer(peers, _peer_of(delta, wallet), -amount)
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
        send_in=send_in,
        send_out=send_out,
        internal_net=internal_net,
        internal_count=internal_count,
        other_counts=tuple(sorted(other_counts.items())),
        event_count=event_count,
        peer_flows=tuple(
            (addr, net, int(count))
            for addr, (net, count) in sorted(
                peers.items(), key=lambda kv: -abs(kv[1][0])
            )[:3]
        ),
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
    if summary.internal_count:
        parts.append(
            f"内部划转 {summary.internal_net:+,.2f} × {summary.internal_count} 笔"
            f"（现货↔合约，不算充提）"
        )
    if summary.other_counts:
        shown = list(summary.other_counts)[:4]
        detail = "、".join(f"{kind} × {count}" for kind, count in shown)
        if len(summary.other_counts) > len(shown):
            detail += f" 等 {len(summary.other_counts)} 类"
        if abs(summary.other_net) > 0.005:
            # A deposit paid with ``send`` has no ``deposit`` row, so both legs
            # are named explicitly. The sign belongs in the direction word, not
            # twice: "转出 +$278,892" reads as a contradiction.
            for sign, total in ((1, summary.send_in), (-1, summary.send_out)):
                if total <= 0.005:
                    continue
                peers = [
                    f"{SYSTEM_ADDRESSES.get(addr) or short_address(addr)}"
                    f"{'' if count == 1 else f' ×{count}'}"
                    for addr, net, count in summary.peer_flows
                    if net * sign > 0
                ]
                verb = "外部转入" if sign > 0 else "外部转出"
                suffix = f"（{'、'.join(peers[:2])}）" if peers else ""
                parts.append(f"{verb} ${total:,.2f}{suffix}")
        parts.append(f"流水类型：{detail}")
    return "；".join(parts)


def event_key(update: Mapping[str, Any]) -> str:
    """Stable identity for one ledger row, used to de-duplicate overlapping scans.

    The poll windows deliberately overlap by a minute or more to absorb index
    lag, so the same row is returned several times. ``nonce`` is unique per
    event when present; otherwise fall back to the timestamp plus size, which
    is still specific enough to avoid swallowing two distinct transfers.
    """
    if not isinstance(update, Mapping):
        return ""
    delta = update.get("delta")
    if not isinstance(delta, Mapping):
        return ""
    kind = str(delta.get("type", "unknown"))
    nonce = delta.get("nonce")
    if nonce not in (None, ""):
        return f"{kind}:{nonce}"
    size = delta.get("usdc") or delta.get("amount") or ""
    return f"{kind}:{update.get('time', '')}:{size}"


@dataclass(frozen=True)
class LedgerFlow:
    """A cash movement read straight from the exchange ledger.

    This is the primary signal because the ledger covers the spot book as well
    as perp, while the equity residual can only see the perp account. The
    amount is the signed external cash flow of the window: deposits minus
    withdrawals plus direction-resolved peer transfers.
    """

    net_flow: float
    deposits: float
    withdrawals: float
    from_ms: int
    to_ms: int
    equity: float | None = None

    @property
    def amount(self) -> float:
        return abs(self.net_flow)

    @property
    def direction(self) -> str:
        return "in" if self.net_flow > 0 else "out"

    @property
    def is_inflow(self) -> bool:
        return self.net_flow > 0

    @property
    def flow_pct(self) -> float | None:
        if not self.equity or self.equity <= 0:
            return None
        return self.net_flow / self.equity * 100.0


def detect_ledger_flow(
    summary: LedgerSummary,
    *,
    min_usd: float,
    from_ms: int,
    to_ms: int,
    equity: float | None = None,
) -> LedgerFlow | None:
    """Return the window's external cash flow when it clears the dollar floor.

    Deposits and withdrawals are summed with the direction-resolved peer
    transfers, because a deposit made with ``send`` never produces a
    ``deposit`` row. Self-transfers between the spot and perp books are already
    excluded from ``other_net`` so they cannot inflate this number.
    """
    if not summary.ok or summary.event_count == 0:
        return None
    net = summary.chain_net + summary.other_net
    if abs(net) < max(0.0, min_usd):
        return None
    return LedgerFlow(
        net_flow=net,
        deposits=summary.deposits,
        withdrawals=summary.withdrawals,
        from_ms=from_ms,
        to_ms=to_ms,
        equity=equity,
    )
