import asyncio
import html
import os
import tempfile
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import re
from typing import Any, Optional
from zoneinfo import ZoneInfo
from loguru import logger
from config.settings import normalize_pnl_filter, settings
from utils.logger import setup_logger
from utils import copy_ledger
from utils.equity_flow import (
    EquitySnapshot,
    analyze_equity_flow,
    ledger_lines,
    summarize_ledger_updates,
)
from hyperliquid.client import HyperliquidClient
from hyperliquid.websocket import HyperliquidWebSocket
from hyperliquid.models import WebSocketUpdate, PositionSide, OrderSide
from copy_engine import WalletMonitor, TradeExecutor, PositionSizer
from version import build_stamp

# Setup logging
setup_logger(settings.log_file, settings.log_level)

# Hyperliquid minimum order size requirement
MIN_POSITION_SIZE_USD = 10.0


def cap_position_value(requested_value: float, *hard_limits: Optional[float]) -> float:
    """Cap a requested notional by the exchange limits and the position cap.

    ``None`` means "no cap" (``MAX_POSITION_SIZE=x``), so it is dropped instead
    of being treated as zero — the previous code compared against a float and
    made every position vanish as soon as the cap was unset.
    """
    limits = [requested_value]
    limits.extend(limit for limit in hard_limits if limit is not None)
    return min(limits)


def remaining_position_budget(existing_value: float = 0.0) -> Optional[float]:
    """Return the leftover per-position budget, or ``None`` when uncapped."""
    cap = settings.sizing.max_position_size
    if cap is None:
        return None
    return max(0.0, cap - existing_value)


def _format_limit(value: Optional[float]) -> str:
    return "unlimited" if value is None else f"${value:,.2f}"

# Initialize components
monitor: WalletMonitor = None
executor: TradeExecutor = None
position_sizer: PositionSizer = None
client: HyperliquidClient = None
telegram_bot: Any = None
notifier: Any = None
target_switch_lock = asyncio.Lock()

# State tracking
is_paused = False
is_stopped = False
# Live mode started with a zero-equity follower wallet: copying is stopped and
# the account-touching commands are refused until /start sees real money.
wallet_unfunded = False
# When the pause started, so reports can say how long copying has been off.
paused_since: Optional[datetime] = None
# Set by /start to wake run_target_monitor out of its post-/stop wait, so a
# stopped bot can resume copying without restarting the container.
restart_requested = asyncio.Event()
trades_copied_count = 0
bot_start_time = None

# Simulated account tracking
simulated_balance = 0.0
simulated_positions = {}  # symbol -> {'size': float, 'entry_price': float, 'side': str}
simulated_pnl = 0.0
processed_fill_ids: set[str] = set()
MAX_PROCESSED_FILL_IDS = 10_000
# A single target limit order can be reported as many exchange fills.  The
# runtime report counts copied target orders, so keep one counter entry per
# target oid instead of incrementing once for every partial fill.
counted_mirror_order_ids: set[str] = set()
# target oid -> follower order metadata. Keeping this mapping in memory is
# sufficient because the target websocket is the source of truth for this run;
# a restart takes a fresh baseline and never cancels unrelated follower orders.
mirrored_orders: dict[str, dict] = {}
# A burst of small target orders can exhaust the exchange action bucket. Queue
# matching intents briefly, then mirror their aggregate as one follower order.
pending_order_batches: dict[str, list[dict]] = {}
pending_order_tasks: dict[str, asyncio.Task] = {}
pending_target_order_ids: set[str] = set()
pending_target_fills: dict[str, list[dict]] = {}
# Serializes follower exchange actions with /stop. Without this, a mirror
# coroutine that passed its stop check could submit just after /stop cancels.
trade_execution_lock = asyncio.Lock()
# Serializes the /copyloss command; a double tap must not double the position.
copy_existing_lock = asyncio.Lock()
# Serializes the /close command so a double tap cannot send duplicate
# reduce-only orders while the first batch is still filling.
close_positions_lock = asyncio.Lock()
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# Target fund-flow monitoring: the last account snapshot and alert throttling.
# Kept in memory on purpose - after a restart there is no trustworthy baseline
# for what happened while the process was down, so the first poll only seeds it.
last_target_equity_snapshot: Optional[EquitySnapshot] = None
target_flow_next_alert_at: float = 0.0
target_flow_last_check_at: Optional[datetime] = None
target_flow_last_equity: Optional[float] = None


def perp_dex_for_symbol(symbol: str) -> str:
    """Return the DEX that owns a perp symbol's collateral and positions."""
    if ":" in symbol:
        return symbol.split(":", 1)[0].lower()
    return ""


def position_unrealized_pnl(position) -> float:
    """Unrealized PnL of one target position, preferring the exchange value.

    Falls back to a price-based estimate when the venue omits ``unrealizedPnl``.
    ``current_price`` is derived from ``positionValue``, whose sign convention
    differs between long and short positions, so the magnitude is used.
    """
    pnl = float(getattr(position, "unrealized_pnl", 0.0) or 0.0)
    if pnl != 0.0:
        return pnl

    entry = float(getattr(position, "entry_price", 0.0) or 0.0)
    mark = abs(float(getattr(position, "current_price", 0.0) or 0.0))
    size = abs(float(getattr(position, "size", 0.0) or 0.0))
    if entry <= 0 or mark <= 0 or size <= 0:
        return 0.0

    signed_size = size if position.side == PositionSide.LONG else -size
    return (mark - entry) * signed_size


def select_existing_positions(positions, pnl_filter: str = "all") -> list:
    """Return the existing target positions that may be copied at startup.

    ``all`` keeps the previous behaviour (every open position). ``loss`` only
    keeps positions the target is currently underwater on: entering the same
    side now gives the follower a better average price than the target has.
    ``profit`` keeps the winners, which means paying a worse price than the
    target — useful only for mirroring a momentum book.
    """
    selected = []
    skipped_in_profit = 0
    skipped_at_loss = 0

    for position in positions or []:
        pnl = position_unrealized_pnl(position)
        if pnl_filter == "loss" and pnl >= 0:
            skipped_in_profit += 1
            continue
        if pnl_filter == "profit" and pnl <= 0:
            skipped_at_loss += 1
            continue
        selected.append(position)

    if pnl_filter == "loss" and skipped_in_profit:
        logger.warning(
            f"PnL filter 'loss': skipping {skipped_in_profit} existing position(s) "
            "the target is not losing on"
        )
    if pnl_filter == "profit" and skipped_at_loss:
        logger.warning(
            f"PnL filter 'profit': skipping {skipped_at_loss} existing position(s) "
            "the target is not profiting on"
        )

    return selected


def copying_status() -> str:
    """Describe whether new target activity is being copied right now.

    Reports are sent on a timer, so a paused bot used to look identical to a
    running one. Keeping the label in one place means the hourly report and any
    other surface cannot drift apart.
    """
    if is_stopped:
        return "🔴 已停止（不再复制任何成交）"
    if is_paused:
        since = ""
        if paused_since is not None:
            since = (
                "，自 "
                + paused_since.astimezone(SHANGHAI_TZ).strftime("%m-%d %H:%M")
                + " 起"
            )
        return f"⏸️ 暂停跟单{since}（持仓保留，不复制新成交与挂单）"
    return "🟢 正在跟单"


def existing_position_policy_text() -> str:
    """Describe how the target's already-open positions are handled.

    Nothing is mirrored at startup any more — that path placed real orders
    without asking. The text therefore always reports "manual"; the PnL filter
    only decides which scope /copyloss lists by default.
    """
    rule = settings.copy_rules.copy_open_pnl_filter
    scope = {"loss": "浮亏", "profit": "浮盈"}.get(rule, "全部")
    return f"手动：/copyloss 选择{scope}仓位（启动不自动追入）"


def unfunded_block_text() -> Optional[str]:
    """Why account-facing commands are refused, or ``None`` when funded.

    A live wallet with $0 cannot place or close anything, so /orders,
    /copyloss and /close are all refused while ``wallet_unfunded`` is set.
    Everything read-only (/status, /pnl, /wallet, /follow, ...) keeps working:
    the operator needs those to see what is going on.
    """
    if not wallet_unfunded:
        return None
    address = settings.hyperliquid.wallet_address or "(未配置)"
    return (
        "💸 <b>跟随钱包没有资金，跟单已停止</b>\n\n"
        f"钱包：<code>{address}</code>\n"
        "净值：<b>$0.00</b>\n\n"
        "该状态下 /orders、/copyloss、/close 均不可用（没有资金既开不了仓也平不了仓）。\n"
        "入金后发 /start 重新检查，资金到账即恢复跟单。"
    )


def calculate_scaled_close_size(
    target_fill_size: float,
    target_pre_close_size: float,
    follower_size: float,
) -> tuple[float, float]:
    """Scale follower reductions by the actual target position reduction."""
    if target_fill_size <= 0 or target_pre_close_size <= 0 or follower_size <= 0:
        return 0.0, 0.0
    close_ratio = min(1.0, target_fill_size / target_pre_close_size)
    return min(follower_size, follower_size * close_ratio), close_ratio


async def copy_existing_position_snapshot(
    pos,
    auto_ratio: float,
    index: int,
    total: int,
    force: bool = False,
) -> dict[str, Any]:
    """Copy one currently-open target position at the live market price.

    Returns a receipt record instead of a bare bool: the Telegram receipt has
    to report the size, leverage and order id that actually went out, and
    "failed" alone leaves the user with nothing to check against the exchange.

    Two independent duplicate guards run here, immediately before the order and
    not once per batch: the follower's live book (read from the same call that
    prices the order) and the on-disk ledger of earlier manual copies.
    """
    target_position_value = abs(pos.size) * pos.entry_price
    requested_position_value = target_position_value * auto_ratio
    record: dict[str, Any] = {
        "symbol": pos.symbol,
        "side": pos.side.value.upper(),
        "ok": False,
        "blocked": False,
        "reason": "",
        "size": 0.0,
        "notional": requested_position_value,
        "requested_notional": requested_position_value,
        "leverage": 0,
        "order_id": None,
        "target_value": target_position_value,
        "target_pnl": position_unrealized_pnl(pos),
        "fill_price": None,
        "fill_size": None,
    }

    follower_dex_state = None
    if not settings.simulated_trading:
        current_follower_state = await client.get_user_state(
            settings.hyperliquid.wallet_address,
            dex=perp_dex_for_symbol(pos.symbol),
        )
        if current_follower_state is None:
            logger.error(f"   ❌ Cannot read follower wallet before copying {pos.symbol}")
            record["reason"] = "无法读取跟随钱包状态，已取消（宁可不买也不重复买）"
            return record
        follower_dex_state = current_follower_state
        held = {
            position.symbol: position.side.value.upper()
            for position in current_follower_state.positions
            if abs(position.size) > 0
        }
    else:
        held = await _follower_positions_by_symbol() or {}

    blocked = copy_guard_block(pos, held, force)
    if blocked:
        logger.warning(f"   ⏭  Skipping {pos.symbol}: {blocked['text']}")
        record["blocked"] = True
        record["reason"] = blocked["text"]
        return record

    your_leverage = calculate_adjusted_leverage(
        target_leverage=pos.leverage,
        adjustment_ratio=settings.leverage.adjustment_ratio,
        symbol=pos.symbol,
        max_leverage=await executor.get_max_leverage(pos.symbol),
    )
    record["leverage"] = your_leverage

    order_side = OrderSide.BUY if pos.side == PositionSide.LONG else OrderSide.SELL
    max_order_notional = await get_follower_order_notional_cap(
        pos.symbol,
        order_side,
        your_leverage,
        dex_state=follower_dex_state,
        prepare_leverage=True,
    )
    if max_order_notional is None:
        logger.error(f"   ❌ Cannot read available order capacity for {pos.symbol}")
        record["reason"] = "无法读取可交易额度"
        return record

    capped_position_value = cap_position_value(
        requested_position_value,
        max_order_notional,
        settings.sizing.max_position_size,
    )
    record["notional"] = capped_position_value
    if capped_position_value < MIN_POSITION_SIZE_USD:
        logger.warning(
            f"⚠️  Skipping Position {index}/{total}: {pos.symbol}; "
            f"allowed value ${capped_position_value:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}"
        )
        record["reason"] = (
            f"可下单金额 ${capped_position_value:.2f} 低于交易所最低 "
            f"${MIN_POSITION_SIZE_USD:.2f}"
        )
        return record
    if capped_position_value < requested_position_value:
        logger.warning(
            f"⚠️  Capping {pos.symbol} copy from "
            f"${requested_position_value:,.2f} to ${capped_position_value:,.2f} "
            f"(available-margin or position-cap limit)"
        )

    your_position_value = capped_position_value
    your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
    margin_needed = your_position_value / your_leverage

    logger.info("")
    logger.info(f"📊 Copying Position {index}/{total}: {pos.symbol}")
    logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
    logger.info(f"   Target Value: ${target_position_value:,.2f}")
    logger.info(f"   Target Unrealized PnL: ${position_unrealized_pnl(pos):,.2f}")
    logger.success(f"   → Your Size: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
    logger.success(f"   → Your Value: ${your_position_value:,.2f}")
    logger.success(f"   → Margin: ${margin_needed:,.2f}")

    result = await executor.execute_market_order(
        symbol=pos.symbol,
        side=order_side,
        size=your_size,
        leverage=your_leverage,
    )
    if not result:
        detail = (getattr(executor, "last_error", "") or "").strip()
        logger.error(f"   ❌ Failed to copy position: {detail or 'no detail'}")
        record["reason"] = f"下单失败：{detail}" if detail else "下单失败（见容器日志）"
        return record

    record["ok"] = True
    record["size"] = float(your_size)
    record["notional"] = your_position_value
    record["order_id"] = str(result)
    # Arm the guard before anything else can fail: the order is out, so the
    # next /copyloss (or a restart) must know about it even if the venue has
    # not reported the position yet.
    copy_ledger.record(
        pos.symbol,
        pos.side.value.upper(),
        your_position_value,
        record["order_id"],
        simulated=settings.simulated_trading,
    )

    if settings.simulated_trading:
        simulated_positions[pos.symbol] = {
            'size': your_size if pos.side == PositionSide.LONG else -your_size,
            'entry_price': pos.entry_price,
            'side': pos.side.value.upper(),
            'leverage': your_leverage,
            'value': your_position_value,
            'margin_used': margin_needed
        }

    logger.success("   ✅ Position copied successfully!")
    return record


def _copy_receipt_text(
    records: list[dict[str, Any]],
    scope_name: str,
    simulated: bool,
) -> str:
    """Render the per-position receipt for a manual copy run."""
    stamp = datetime.now(SHANGHAI_TZ).strftime("%m-%d %H:%M:%S")
    filled = [record for record in records if record["ok"]]
    total_notional = sum(record["notional"] for record in filled)
    lines = [
        f"🧾 <b>一键复制回执</b>｜{scope_name}｜{'模拟' if simulated else '实盘'}｜{stamp}",
        "",
    ]
    for index, record in enumerate(records, 1):
        symbol = html.escape(str(record["symbol"]))
        side = record["side"]
        if not record["ok"]:
            reason = html.escape(str(record["reason"] or "未知原因"))
            lines.append(f"{index}. ❌ <b>{symbol} {side}</b>")
            lines.append(f"   未成交：{reason}")
            continue
        size = record["fill_size"] or record["size"]
        price = record["fill_price"]
        if price:
            price_text = f"@ ${price:,.4f}"
        elif simulated:
            price_text = "（模拟，按当时市价）"
        else:
            price_text = "（未取到成交价，以交易所为准）"
        lines.append(
            f"{index}. ✅ <b>{symbol} {side}</b>｜{record['leverage']}x"
        )
        lines.append(
            f"   成交 {size:.6g} {price_text}｜名义 ${record['notional']:,.2f}"
        )
        if record["order_id"]:
            lines.append(f"   订单 <code>{html.escape(str(record['order_id']))}</code>")
    lines.append("")
    lines.append(
        f"合计：成功 {len(filled)}/{len(records)}｜成交名义 ${total_notional:,.2f}"
    )
    blocked = [record for record in records if record.get("blocked")]
    if blocked:
        lines.append(
            f"下单检查拦下 {len(blocked)} 笔（已持有 / 刚复制过），这些没有下单。"
        )
    return "\n".join(lines)


async def _recent_fills_by_symbol(started_at_ms: int) -> dict[str, dict]:
    """Follower fills created after ``started_at_ms``, keyed by symbol."""
    if settings.simulated_trading:
        return {}
    wallet = settings.hyperliquid.wallet_address
    if not wallet:
        return {}
    fills: dict[str, dict] = {}
    for fill in await client.get_user_fills(wallet, limit=20):
        if fill["timestamp"] < started_at_ms:
            continue
        fills.setdefault(fill["symbol"], fill)
        fills.setdefault(fill["symbol"].lower(), fill)
    return fills


async def handle_copy_existing(
    mode: Optional[str] = None,
    confirm: bool = False,
    selection: Optional[list[int]] = None,
    force: bool = False,
) -> str:
    """Copy the target's currently open positions on demand, via Telegram.

    Manual by design: nothing is mirrored at startup any more. The preview
    numbers every candidate, ``selection`` (1-based) takes a subset, and every
    run ends with a per-position receipt, because the previous auto-copy path
    placed real orders that the user only noticed on the exchange.

    Every candidate passes :func:`copy_guard_block` twice: once for the preview
    (so the list shows what will be skipped before anything is confirmed) and
    again inside the snapshot immediately before the order, on a freshly read
    follower book. ``force`` relaxes the "already copied" rules for deliberate
    adds; an opposite-side position still blocks.
    """
    if is_stopped:
        return "🔴 机器人已停止，无法执行。请重启容器后再使用。"
    blocked = unfunded_block_text()
    if blocked:
        return blocked
    if not client or not executor or not monitor:
        return "❌ 机器人尚未就绪，请稍后再试。"

    try:
        rule = normalize_pnl_filter(
            mode or settings.copy_rules.copy_open_pnl_filter
        )
    except ValueError:
        return (
            "❌ 无效的范围参数。\n\n"
            "用法：<code>/copyloss [loss|all|profit] [序号...] [confirm]</code>\n"
            "默认 <code>loss</code>：只列出目标当前浮亏的仓位。"
        )

    state = await monitor.get_current_state()
    if not state:
        return "❌ 无法读取目标钱包当前状态，请稍后再试。"

    target_equity = await client.get_portfolio_account_value(settings.target_wallet)
    if not target_equity or target_equity <= 0:
        return "❌ 无法读取目标钱包净值，无法计算跟单比例。"

    if settings.simulated_trading:
        follower_equity = simulated_balance
    else:
        follower_equity = await get_follower_balance()
    if not follower_equity or follower_equity <= 0:
        return "❌ 无法读取跟随钱包净值，无法计算跟单比例。"

    auto_ratio = follower_equity / target_equity
    candidates = select_existing_positions(state.positions, rule)
    scope_name = {"loss": "浮亏", "profit": "浮盈", "all": "全部"}[rule]
    if not candidates:
        return (
            f"ℹ️ 目标当前没有{scope_name}仓位"
            f"（共 {len(state.positions)} 个持仓），无需操作。"
        )

    held = await _follower_positions_by_symbol()
    if held is None:
        # Fail closed: an unreadable book used to look like "holds nothing",
        # which is exactly how a duplicate buy can slip through.
        return (
            "❌ 无法读取跟随钱包当前持仓，为避免重复下单已取消。\n"
            "请稍后重试；若持续失败，检查 API 连接或钱包地址配置。"
        )

    # Number the whole candidate list, including symbols the guard will skip:
    # numbering only the copyable subset shifted every index after the first
    # copy, so re-sending a number silently hit a different position.
    guard: dict[str, dict[str, str]] = {}
    for pos in candidates:
        block = copy_guard_block(pos, held, force)
        if block:
            guard[pos.symbol] = block
    copyable = [pos for pos in candidates if pos.symbol not in guard]
    if not copyable:
        head = (
            f"ℹ️ 目标当前的{scope_name}仓位（{len(candidates)} 个）全部命中下单检查，"
            "本次不会下任何单："
        )
        rows = [
            f"• {html.escape(pos.symbol)}：{html.escape(guard[pos.symbol]['text'])}"
            for pos in candidates
        ]
        tail = "" if force else "\n\n确实要再买一次，请追加 <code>force</code>（反向持仓除外）。"
        return "\n".join([head, ""] + rows) + tail

    chosen = copyable
    if selection:
        wanted: list[int] = []
        for number in selection:
            if number not in wanted:
                wanted.append(number)
        invalid = [number for number in wanted if not 1 <= number <= len(candidates)]
        if invalid:
            listed = "、".join(str(number) for number in invalid)
            return (
                f"❌ 序号超出范围：{listed}\n"
                f"当前可选 1-{len(candidates)}。发送 <code>/copyloss {rule}</code> 查看列表。"
            )
        chosen = [candidates[number - 1] for number in wanted]

    ratio_text = f"1:{int(1 / auto_ratio)}" if auto_ratio > 0 else "0"
    lines = [
        f"🎯 <b>目标{scope_name}仓位{'（预览）' if not confirm else ''}</b>",
        "",
        f"目标净值 ${target_equity:,.2f}｜跟随净值 ${follower_equity:,.2f}"
        f"（比例 {ratio_text}）",
        "",
    ]
    total_your_value = 0.0
    selected_numbers = {id(pos) for pos in chosen}
    for index, pos in enumerate(candidates, 1):
        target_value = abs(pos.size) * pos.entry_price
        your_value = target_value * auto_ratio
        total_your_value += your_value
        pnl = position_unrealized_pnl(pos)
        picked = " ✅" if id(pos) in selected_numbers and selection else ""
        blocked = guard.get(pos.symbol)
        guard_note = blocked["short"] if blocked else ""
        below_min = (
            f"｜⚠️ 低于 ${MIN_POSITION_SIZE_USD:.0f} 最低，将被跳过"
            if your_value < MIN_POSITION_SIZE_USD and not guard_note
            else ""
        )
        lines.append(
            f"{index}. {html.escape(pos.symbol)} {pos.side.value.upper()}｜"
            f"目标 ${target_value:,.0f}｜你 ≈ ${your_value:,.0f}｜"
            f"PnL ${pnl:+,.0f}｜{pos.leverage}x{picked}{below_min}{guard_note}"
        )
    lines.append("")
    lines.append(f"全部复制合计 ≈ ${total_your_value:,.2f}。")

    if not confirm:
        selected_total = sum(
            abs(pos.size) * pos.entry_price * auto_ratio
            for pos in chosen
            if pos.symbol not in guard
        )
        if selection:
            numbers = " ".join(
                str(next(i for i, row in enumerate(candidates, 1) if row is pos))
                for pos in chosen
            )
            blocked_chosen = [
                pos.symbol for pos in chosen if pos.symbol in guard
            ]
            lines.append(
                f"已选 {len(chosen) - len(blocked_chosen)} 笔，"
                f"合计 ≈ ${selected_total:,.2f}。"
            )
            if blocked_chosen:
                lines.append(
                    "其中会被下单检查跳过："
                    + "、".join(html.escape(symbol) for symbol in blocked_chosen)
                )
            lines.append(
                f"发送 <code>/copyloss {rule} {numbers} confirm</code> 执行。"
            )
        else:
            sample = " ".join(
                str(i) for i in range(1, min(len(candidates), 2) + 1)
            )
            lines.append(
                f"按序号复制：<code>/copyloss {rule} {sample} confirm</code>"
                f"（只买这几笔）；全部执行：<code>/copyloss {rule} confirm</code>。"
            )
        lines.append("下单前不会发送任何订单。")
        return "\n".join(lines)

    if copy_existing_lock.locked():
        return "⏳ 上一次一键复制仍在执行，请稍后再试。"

    started_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 5000
    async with copy_existing_lock:
        records: list[dict[str, Any]] = []
        for index, pos in enumerate(chosen, 1):
            # No batch-level shortcut: the guard runs inside the snapshot, on
            # the same live read that prices the order, so a stale list cannot
            # let a second buy through.
            try:
                records.append(
                    await copy_existing_position_snapshot(
                        pos, auto_ratio, index, len(chosen), force=force
                    )
                )
            except Exception as exc:
                logger.exception(f"/copyloss failed for {pos.symbol}: {exc}")
                records.append({
                    "symbol": pos.symbol,
                    "side": pos.side.value.upper(),
                    "ok": False,
                    "blocked": False,
                    "reason": f"异常：{exc}",
                    "size": 0.0,
                    "notional": 0.0,
                    "requested_notional": 0.0,
                    "leverage": 0,
                    "order_id": None,
                    "fill_price": None,
                    "fill_size": None,
                })

    fills = await _recent_fills_by_symbol(started_at_ms)
    for record in records:
        if not record["ok"]:
            continue
        fill = fills.get(record["symbol"]) or fills.get(record["symbol"].lower())
        if fill:
            record["fill_price"] = fill["price"]
            record["fill_size"] = fill["size"]

    receipt = _copy_receipt_text(records, scope_name, settings.simulated_trading)
    notes: list[str] = []
    if is_paused:
        notes.append("⚠️ 机器人处于暂停状态：本次是手动下单，自动跟单仍未恢复。")
    rejected = [
        record
        for record in records
        if not record["ok"] and not record.get("blocked")
    ]
    if rejected:
        notes.append("失败项的原因见上；下单被拒时交易所原文在容器日志里。")
    if force and any(record["ok"] for record in records):
        notes.append("⚠️ 本次使用了 force：已绕过“重复复制”检查，仓位可能被叠加。")
    return receipt + ("\n\n" + "\n".join(notes) if notes else "")


async def _follower_positions_by_symbol() -> Optional[dict[str, str]]:
    """Follower positions as ``symbol -> side``, or ``None`` if unreadable.

    ``None`` is deliberately distinct from ``{}``: a failed read used to look
    like "holds nothing", which is how a duplicate buy could slip through. The
    caller must refuse to order when the book cannot be read.
    """
    if settings.simulated_trading:
        held: dict[str, str] = {}
        for symbol, data in simulated_positions.items():
            size = float(data.get("size", 0.0) or 0.0)
            if size == 0:
                continue
            # Older writers stored only the signed size; derive the side from
            # it rather than trusting a key that may be missing.
            side = str(data.get("side") or ("LONG" if size > 0 else "SHORT"))
            held[symbol] = side.upper()
        return held
    follower_state = await _get_follower_state()
    if not follower_state:
        return None
    return {
        position.symbol: position.side.value.upper()
        for position in follower_state.positions
        if abs(position.size) > 0
    }


def copy_guard_block(
    pos,
    held: dict[str, str],
    force: bool = False,
) -> Optional[dict[str, str]]:
    """Why this position must not be bought again right now, or ``None``.

    Three independent checks, because each catches a different duplicate:

    * the follower already holds the same side -> a repeat would silently add;
    * the follower holds the opposite side -> the order would close or flip it;
    * a copy of the same symbol/side just went through -> the exchange has not
      reported the new position yet, so the position check still says "no".

    Returns ``{"code", "text", "short"}``: ``text`` goes into the receipt, and
    ``short`` into the numbered preview where a full sentence would not fit.

    ``force`` is the escape hatch for deliberately adding to an existing
    position; it does not override the opposite-side case, which needs the old
    position closed first.
    """
    symbol = pos.symbol
    wanted = pos.side.value.upper()
    current = held.get(symbol)

    if current and current != wanted:
        return {
            "code": "opposite",
            "text": f"跟随钱包持有反向 {symbol} {current}，下单会先平掉/反手，已跳过",
            "short": "｜持有反向仓，跳过",
        }
    if current == wanted and not force:
        return {
            "code": "held",
            "text": f"跟随钱包已持有 {symbol} {current}，跳过以免重复加仓",
            "short": "｜已持有，跳过",
        }

    if not force:
        window = settings.copy_rules.copy_duplicate_guard_minutes
        hit = copy_ledger.recent(
            symbol, wanted, window, simulated=settings.simulated_trading
        )
        if hit:
            order = hit.get("order_id")
            suffix = f"，订单 {order}" if order else ""
            minutes = hit["age_minutes"]
            when = "刚刚" if minutes < 1 else f"{minutes:.0f} 分钟前"
            return {
                "code": "recent",
                "text": (
                    f"{when}复制过 {symbol} {wanted}{suffix}"
                    f"（{window} 分钟内不重复买）"
                ),
                "short": f"｜{when}复制过，跳过",
            }
    return None


def configure_monitor_callbacks(wallet_monitor: WalletMonitor) -> None:
    """Attach the process-level handlers to a target wallet monitor."""
    # Fills are the real-time source of truth. Position callbacks describe the
    # same target action and would otherwise submit duplicate mirror orders.
    wallet_monitor.on_new_position = None
    wallet_monitor.on_position_close = None
    wallet_monitor.on_position_update = None
    wallet_monitor.on_new_order = on_new_order
    wallet_monitor.on_order_update = on_order_update
    wallet_monitor.on_order_cancel = on_order_cancel
    wallet_monitor.on_order_fill = on_order_fill


def persist_target_wallet(address: str) -> None:
    """Persist the active target address in the project-level .env file."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    original_exists = env_path.exists()
    original_mode = env_path.stat().st_mode & 0o777 if original_exists else 0o600
    content = env_path.read_text(encoding="utf-8") if original_exists else ""
    lines = content.splitlines(keepends=True)
    assignment = re.compile(r"^(\s*TARGET_WALLET_ADDRESS\s*=).*?(\r?\n)?$")
    replaced = False

    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        match = assignment.match(line)
        if not match:
            continue
        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
        lines[index] = f"{match.group(1)}{address}{newline}"
        replaced = True
        break

    if not replaced:
        if lines and not content.endswith(("\n", "\r")):
            lines.append("\n")
        lines.append(f"TARGET_WALLET_ADDRESS={address}\n")

    updated = "".join(lines)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=".env.", suffix=".tmp", dir=env_path.parent
    )
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as temp_file:
            temp_file.write(updated)
        os.chmod(temp_name, original_mode)
        os.replace(temp_name, env_path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


async def get_follower_balance() -> float | None:
    """Return the balance used for live sizing, or the simulated balance."""
    if settings.simulated_trading:
        return simulated_balance
    if not settings.hyperliquid.wallet_address:
        logger.error("Live sizing requires HYPERLIQUID_WALLET_ADDRESS")
        return None
    return await client.get_portfolio_account_value(settings.hyperliquid.wallet_address)


async def get_follower_order_notional_cap(
    symbol: str,
    side: OrderSide,
    leverage: float,
    dex_state=None,
    prepare_leverage: bool = False,
) -> float | None:
    """Return the configured per-order notional cap for the follower wallet.

    Standard accounts use the selected Perp DEX's free margin. Unified
    accounts use Hyperliquid's market- and direction-specific available
    trading notional because individual Perp DEX balances are not authoritative
    for unified accounts.
    """
    usage_ratio = settings.copy_rules.max_margin_usage_ratio
    if settings.simulated_trading:
        return max(0.0, simulated_balance) * max(1.0, leverage) * usage_ratio

    address = settings.hyperliquid.wallet_address
    if not address:
        return None

    if settings.hyperliquid.account_mode == "unified":
        if prepare_leverage and not await executor.prepare_leverage(
            symbol, int(max(1, leverage))
        ):
            return None
        available_notional = await client.get_available_to_trade_notional(
            address,
            symbol,
            side.value,
            target_leverage=max(1, leverage),
        )
        if available_notional is None:
            return None
        return max(0.0, available_notional) * usage_ratio

    if dex_state is None:
        dex_state = await client.get_user_state(
            address, dex=perp_dex_for_symbol(symbol)
        )
    if dex_state is None:
        return None
    return max(0.0, dex_state.available_balance) * max(1.0, leverage) * usage_ratio


def calculate_adjusted_leverage(
    target_leverage: float,
    adjustment_ratio: float,
    symbol: str,
    max_leverage: float | None = None,
) -> int:
    """
    Calculate adjusted leverage with proper rounding and max leverage limits.
    
    Hyperliquid only supports integer leverage (1x, 2x, 3x, etc.)
    Each asset has different max leverage limits.

    Args:
        target_leverage: Target wallet's leverage
        adjustment_ratio: Multiplier (e.g., 0.5 = use 50% of target's leverage)
        symbol: Trading symbol (for max leverage lookup)
        max_leverage: Exchange-declared ceiling for this asset. HIP-3 builder
            DEX assets often cap well below 10x, so the static table below is
            only a fallback when the exchange metadata is unavailable.

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
    
    # Exchange metadata wins: a HIP-3 asset can declare 3x while the static
    # table would otherwise let the copier request 10x and get rejected.
    if max_leverage is not None and max_leverage > 0:
        asset_max_leverage = int(max_leverage)
    else:
        asset_max_leverage = MAX_LEVERAGE_LIMITS.get(symbol.upper(), 10)
    
    # Calculate desired leverage
    desired_leverage = target_leverage * adjustment_ratio
    
    # Round to nearest integer
    rounded_leverage = round(desired_leverage)
    
    # Ensure minimum of 1x
    rounded_leverage = max(1, rounded_leverage)
    
    # Cap at asset's max leverage
    final_leverage = min(rounded_leverage, asset_max_leverage)
    
    return final_leverage


async def on_new_position(position_data: dict):
    """
    Called when target wallet opens a new position
    This is where we copy the trade!
    """
    global trades_copied_count, is_paused, paused_since, simulated_balance, simulated_positions, simulated_pnl
    
    # Check if paused
    if is_paused:
        logger.warning("⏸️ Bot is paused - skipping trade")
        return
    
    # Check max open trades limit against the follower's own positions.
    # This used to count the target's positions, so a target holding one
    # position blocked every new copy — the opposite of a follower-side cap.
    if settings.copy_rules.max_open_trades is not None:
        if settings.simulated_trading:
            current_trades = len(simulated_positions)
        else:
            follower_state = await client.get_user_state(
                settings.hyperliquid.wallet_address
            )
            current_trades = len(follower_state.positions) if follower_state else 0
        if current_trades >= settings.copy_rules.max_open_trades:
            logger.warning(f"⚠️ Max open trades limit reached ({current_trades}/{settings.copy_rules.max_open_trades}) - skipping trade")
            return
    
    # Check account equity limit
    if settings.copy_rules.max_account_equity is not None:
        current_equity = await client.get_portfolio_account_value(settings.target_wallet)
        if current_equity is None:
            logger.error("Unable to read target account equity for MAX_ACCOUNT_EQUITY check")
            return
        if current_equity >= settings.copy_rules.max_account_equity:
            logger.warning(f"⚠️ Max account equity reached (${current_equity:,.2f}/${settings.copy_rules.max_account_equity:,.2f}) - stopping copy trading")
            is_paused = True
            paused_since = datetime.now(timezone.utc)
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
        target_balance = (
            await client.get_portfolio_account_value(settings.target_wallet)
            if target_state else 100000
        )
        if target_balance is None or target_balance <= 0:
            logger.error("Unable to read target wallet total equity; skipping position copy")
            return
        your_balance = await get_follower_balance()
        if your_balance is None or your_balance <= 0:
            logger.error("Unable to read follower wallet total equity; skipping position copy")
            return

        # Calculate your position size from the follower's actual balance.
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


def _pending_order_intent(order_data: dict) -> tuple[str, str, str]:
    """Return a stable merge key that never combines opposite trade intents."""
    symbol = str(order_data.get("coin", "") or "")
    side = order_data.get("side", "")
    side_code = side.value if isinstance(side, OrderSide) else str(side).lower()
    is_buy = side_code in ("b", "buy")
    direction = str(order_data.get("dir", order_data.get("direction", "")) or "").lower()
    reduce_only = bool(
        order_data.get("reduceOnly", order_data.get("reduce_only", False))
        or "close" in direction
        or "reduce" in direction
    )
    if "short" in direction:
        position_side = "short"
    elif "long" in direction:
        position_side = "long"
    elif reduce_only:
        position_side = "short" if is_buy else "long"
    else:
        position_side = "long" if is_buy else "short"
    return symbol, "close" if reduce_only else "open", position_side


async def on_new_order(order_data: dict):
    """Queue a target resting order so dense bursts use one exchange action."""
    if not settings.copy_rules.mirror_pending_orders or is_paused or is_stopped:
        return

    target_oid = str(order_data.get("_target_oid", order_data.get("oid", "")) or "")
    if not target_oid:
        await _mirror_new_order(order_data)
        return
    if target_oid in mirrored_orders or target_oid in pending_target_order_ids:
        return

    key = "|".join(_pending_order_intent(order_data))
    pending_target_order_ids.add(target_oid)
    pending_order_batches.setdefault(key, []).append(order_data)
    previous = pending_order_tasks.get(key)
    if previous and not previous.done():
        previous.cancel()
    pending_order_tasks[key] = asyncio.create_task(_flush_pending_order_batch(key))


async def _flush_pending_order_batch(key: str) -> None:
    """Mirror one aggregate order after the burst has gone quiet."""
    try:
        await asyncio.sleep(settings.copy_rules.pending_order_merge_window_seconds)
    except asyncio.CancelledError:
        return

    orders = pending_order_batches.pop(key, [])
    pending_order_tasks.pop(key, None)
    target_order_ids = [
        str(order.get("_target_oid", order.get("oid", "")) or "")
        for order in orders
    ]
    try:
        if not orders:
            return
        filled_order_ids = {
            target_oid for target_oid in target_order_ids
            if pending_target_fills.get(target_oid)
        }
        resting_orders = [
            order for order in orders
            if str(order.get("_target_oid", order.get("oid", "")) or "")
            not in filled_order_ids
        ]
        if resting_orders:
            await _mirror_pending_order_batch(key, resting_orders)

        fill_events = [
            fill
            for target_oid in filled_order_ids
            for fill in pending_target_fills.pop(target_oid, [])
        ]
        if fill_events:
            pending_target_order_ids.difference_update(filled_order_ids)
            await _copy_pending_order_fills(fill_events)
    finally:
        pending_target_order_ids.difference_update(target_order_ids)
        for target_oid in target_order_ids:
            pending_target_fills.pop(target_oid, None)


async def _mirror_pending_order_batch(key: str, orders: list[dict]) -> None:
    """Create one follower limit order for the still-resting target orders."""
    if len(orders) == 1:
        await _mirror_new_order(orders[0])
        return

    total_size = sum(
        abs(float(order.get("sz", order.get("origSz", 0)) or 0))
        for order in orders
    )
    weighted_notional = sum(
        abs(float(order.get("sz", order.get("origSz", 0)) or 0))
        * float(order.get("limitPx", order.get("px", 0)) or 0)
        for order in orders
    )
    if total_size <= 0 or weighted_notional <= 0:
        await _mirror_new_order(orders[-1])
        return

    target_order_ids = [
        str(order.get("_target_oid", order.get("oid", "")) or "")
        for order in orders
    ]
    batch_id = "merged:" + ",".join(target_order_ids)
    merged_order = {
        **orders[-1],
        "sz": total_size,
        "origSz": total_size,
        "limitPx": weighted_notional / total_size,
        "_target_oid": batch_id,
        "_merged_target_order_ids": target_order_ids,
    }
    logger.info(
        f"Coalescing {len(orders)} target orders into one mirror: "
        f"{key} size={total_size:.8f} price=${merged_order['limitPx']:,.4f}"
    )
    await _mirror_new_order(merged_order)
    mirror = mirrored_orders.get(batch_id)
    if mirror:
        mirror["merged_target_order_ids"] = target_order_ids
        for target_oid in target_order_ids:
            if target_oid:
                mirrored_orders[target_oid] = mirror


async def _copy_pending_order_fills(fills: list[dict]) -> None:
    """Copy fills that arrived before their pending-order batch was flushed."""
    fills.sort(key=lambda fill: int(fill.get("time", 0) or 0))
    if len(fills) == 1:
        await on_order_fill(fills[0])
        return

    total_size = sum(abs(float(fill.get("sz", 0) or 0)) for fill in fills)
    if total_size <= 0:
        return
    weighted_price = sum(
        abs(float(fill.get("sz", 0) or 0)) * float(fill.get("px", 0) or 0)
        for fill in fills
    ) / total_size
    merged_fill = dict(fills[-1])
    merged_fill["oid"] = "merged-fill:" + ",".join(
        str(fill.get("oid", "")) for fill in fills
    )
    merged_fill["tid"] = "merged-fill:" + ",".join(_fill_id(fill) for fill in fills)
    merged_fill["sz"] = total_size
    merged_fill["px"] = weighted_price
    if any("close" in str(fill.get("dir", "")).lower() for fill in fills):
        merged_fill["_target_pre_close_size"] = max(
            float(fill.get("_target_pre_close_size", 0) or 0) for fill in fills
        )
    logger.info(
        f"Coalescing {len(fills)} target fills into one follower execution: "
        f"size={total_size:.8f} price=${weighted_price:,.4f}"
    )
    await on_order_fill(merged_fill)


async def _mirror_new_order(order_data: dict):
    """Mirror a target resting order immediately and remember its oid mapping."""
    global mirrored_orders
    try:
        if not settings.copy_rules.mirror_pending_orders:
            return
        if is_paused or is_stopped:
            logger.warning("Bot is paused or stopped - skipping pending order mirror")
            return

        symbol = order_data.get('coin', '')
        side = order_data.get('side', '')
        target_size = abs(float(order_data.get('sz', order_data.get('origSz', 0))))
        price = float(order_data.get('limitPx', order_data.get('px', 0)) or 0)
        target_oid = str(order_data.get('_target_oid', order_data.get('oid', '')) or '')
        side_code = side.value if isinstance(side, OrderSide) else str(side).lower()
        is_buy = side_code in ('b', 'buy')
        raw_direction = str(
            order_data.get("dir", order_data.get("direction", "")) or ""
        )
        direction_lower = raw_direction.lower()
        is_closing_order = "close" in direction_lower or "reduce" in direction_lower
        if "short" in direction_lower:
            position_side = PositionSide.SHORT
        elif "long" in direction_lower:
            position_side = PositionSide.LONG
        else:
            position_side = PositionSide.LONG if is_buy else PositionSide.SHORT
        reduce_only = bool(
            order_data.get("reduceOnly", order_data.get("reduce_only", False))
            or is_closing_order
        )
        # Use position intent in every mirror notification, not raw order side.
        if reduce_only:
            close_side = (
                position_side
                if "long" in direction_lower or "short" in direction_lower
                else PositionSide.SHORT if is_buy else PositionSide.LONG
            )
            notification_side = f"CLOSE {close_side.value.upper()}"
        else:
            notification_side = f"OPEN {position_side.value.upper()}"

        if target_oid and target_oid in mirrored_orders:
            return
        if not symbol or not target_oid or target_size <= 0 or price <= 0:
            await _notify_copy_failure(
                symbol=symbol or "unknown", direction=notification_side,
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
                    symbol=symbol, direction=notification_side, target_size=target_size,
                    follower_size=0, price=price, category="镜像价格查询失败",
                    reason=str(exc), fill_id=target_oid,
                    stage="镜像交易",
                )
                return
        if settings.copy_rules.max_open_orders is not None:
            active_mirrors = len({
                id(item) for item in mirrored_orders.values() if item.get("active", True)
            })
            if active_mirrors >= settings.copy_rules.max_open_orders:
                await _notify_copy_failure(
                    symbol=symbol, direction=notification_side, target_size=target_size,
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
        if reduce_only and target_position:
            notification_side = f"CLOSE {target_position.side.value.upper()}"
        if settings.copy_rules.auto_adjust_size and not reduce_only:
            follower_balance = await get_follower_balance()
            target_balance = await client.get_portfolio_account_value(settings.target_wallet)
            if follower_balance is None or target_balance is None or target_balance <= 0:
                await _notify_copy_failure(
                    symbol=symbol, direction=notification_side, target_size=target_size,
                    follower_size=0, price=price, category="钱包总资金查询失败",
                    reason="Unable to read portfolio account value for proportional sizing",
                    fill_id=target_oid, stage="镜像交易",
                )
                return
            ratio = follower_balance / target_balance
            our_size = target_size * ratio
        else:
            our_size = target_size

        leverage = calculate_adjusted_leverage(
            target_leverage,
            settings.leverage.adjustment_ratio,
            symbol,
            await executor.get_max_leverage(symbol),
        )
        # Apply the same hard exposure and margin ceilings used by fill copies
        # before sending a live resting order.
        position_cap = settings.sizing.max_position_size
        max_size = position_cap / price if position_cap is not None else float("inf")
        follower_dex_state = None
        if reduce_only:
            if settings.simulated_trading:
                simulated_position = simulated_positions.get(symbol)
                follower_close_size = (
                    abs(simulated_position.get("size", 0))
                    if simulated_position
                    and simulated_position.get("side") == position_side.value
                    else 0.0
                )
            else:
                follower_dex_state = await client.get_user_state(
                    settings.hyperliquid.wallet_address,
                    dex=perp_dex_for_symbol(symbol),
                )
                if follower_dex_state is None:
                    await _notify_copy_failure(
                        symbol=symbol, direction=notification_side, target_size=target_size,
                        follower_size=0, price=price, category="DEX账户查询失败",
                        reason=f"Unable to read {perp_dex_for_symbol(symbol) or 'default'} DEX state",
                        fill_id=target_oid, stage="镜像交易",
                    )
                    return
                follower_position = next(
                    (position for position in follower_dex_state.positions if position.symbol == symbol),
                    None,
                )
                follower_close_size = (
                    follower_position.size
                    if follower_position and follower_position.side == position_side
                    else 0.0
                )
            target_pre_close_size = (
                target_position.size
                if target_position and target_position.side == position_side
                else 0.0
            )
            close_ratio = (
                min(1.0, target_size / target_pre_close_size)
                if target_pre_close_size > 0 else 0.0
            )
            our_size = follower_close_size * close_ratio
        else:
            if not settings.simulated_trading:
                follower_dex_state = await client.get_user_state(
                    settings.hyperliquid.wallet_address,
                    dex=perp_dex_for_symbol(symbol),
                )
                if follower_dex_state is None:
                    await _notify_copy_failure(
                        symbol=symbol, direction=notification_side, target_size=target_size,
                        follower_size=our_size, price=price, category="DEX账户查询失败",
                        reason=f"Unable to read {perp_dex_for_symbol(symbol) or 'default'} DEX state",
                        fill_id=target_oid,
                        stage="镜像交易",
                    )
                    return
            max_order_notional = await get_follower_order_notional_cap(
                symbol,
                OrderSide.BUY if is_buy else OrderSide.SELL,
                leverage,
                dex_state=follower_dex_state,
                prepare_leverage=True,
            )
            if max_order_notional is None:
                await _notify_copy_failure(
                    symbol=symbol, direction=notification_side, target_size=target_size,
                    follower_size=our_size, price=price, category="可交易额度查询失败",
                    reason="Unable to read the follower's available order capacity",
                    fill_id=target_oid, stage="镜像交易",
                )
                return
            max_size = min(max_size, max_order_notional / price)
        our_size = min(our_size, max_size)
        if our_size * price < MIN_POSITION_SIZE_USD:
            await _notify_copy_failure(
                symbol=symbol, direction=notification_side, target_size=target_size,
                follower_size=our_size, price=price, category="低于最小订单金额",
                reason=f"Mirrored order notional ${our_size * price:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}",
                fill_id=target_oid,
                stage="镜像交易",
            )
            return

        async with trade_execution_lock:
            if is_stopped:
                logger.info("Bot stopped while preparing mirror order; no exchange request sent")
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
                symbol=symbol, direction=notification_side, target_size=target_size,
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
                target_oid=target_oid, follower_oid=str(result),
            )
    except Exception as e:
        logger.error(f"Error notifying new target order: {e}")
        try:
            await _notify_copy_failure(
                symbol=str(order_data.get("coin", "unknown")),
                direction=notification_side,
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
    if mirror.get("merged_target_order_ids"):
        logger.info(
            f"Ignoring target cancellation for merged mirror {target_oid}; "
            "the shared follower order remains active"
        )
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
    if is_stopped:
        return
    target_oid = str(order_data.get("_target_oid", order_data.get("oid", "")) or "")
    mirror = mirrored_orders.get(target_oid)
    if mirror:
        if mirror.get("merged_target_order_ids"):
            logger.info(
                f"Ignoring individual price update for merged mirror {target_oid}; "
                "the shared follower order keeps its aggregate price"
            )
            return
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
            follower_total_equity = await client.get_portfolio_account_value(
                settings.hyperliquid.wallet_address
            )
            if follower_total_equity is None:
                logger.error("Unable to read follower wallet equity; skipping fill")
                return
            follower_balance = follower_total_equity

        our_size = position_sizer.calculate_size(
            target_position=target_position,
            target_wallet_balance=(
                await client.get_portfolio_account_value(settings.target_wallet)
                if monitor.current_state else 1000000
            ),
            your_wallet_balance=follower_balance
        )

        if our_size is None:
            logger.warning("Skipping fill because wallet equity could not be read")
            return
        
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
            symbol=symbol,
            max_leverage=await executor.get_max_leverage(symbol),
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
    if notifier and stage != "成交跟单":
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

    if is_stopped:
        logger.info("Bot is stopped - skipping fill copy")
        return

    target_oid = str(fill_data.get("oid", "") or "")
    if target_oid in pending_target_order_ids:
        pending_target_fills.setdefault(target_oid, []).append(fill_data)
        logger.info(
            f"Target fill {target_oid} arrived during pending-order merge; "
            "queueing it to prevent a duplicate follower order"
        )
        return
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
        if target_oid not in counted_mirror_order_ids:
            counted_mirror_order_ids.add(target_oid)
            trades_copied_count += 1
        logger.info(
            f"Target fill acknowledged for mirrored order {target_oid}; "
            "no duplicate follower order will be submitted"
        )
        _remember_fill(_fill_id(fill_data))
        return

    if is_paused:
        logger.warning("Bot is paused - skipping fill copy")
        return

    try:
        symbol = fill_data.get("coin", "")
        direction = str(fill_data.get("dir", "") or "")
        direction_lower = direction.lower()
        target_size = abs(float(fill_data.get("sz", 0)))
        price = float(fill_data.get("px", 0))
        fill_id = _fill_id(fill_data)

        if fill_id in processed_fill_ids:
            logger.info(f"Skipping already copied fill {fill_id}")
            return
        if not symbol or target_size <= 0 or price <= 0:
            logger.warning(f"Skipping malformed fill: {fill_data}")
            return
        # Spot events are not copyable: spot pairs are named "@<index>"
        # (e.g. "@142" = UBTC/USDC) and the exchange also emits
        # "Spot Dust Conversion" when it sweeps sub-dust balances. Both used to
        # fall through to the "unknown fill direction" warning on every sweep.
        if str(symbol).startswith("@") or "spot" in direction_lower or "dust" in direction_lower:
            logger.info(
                f"Ignoring spot event {symbol} ({direction}): only perps are mirrored"
            )
            # Dust conversions carry tid=0; remembering that would poison the
            # dedupe set for every other zero-id fill.
            if fill_id and fill_id != "0":
                _remember_fill(fill_id)
            return
        if ">" in direction:
            logger.warning(f"Skipping position flip until it can be reconciled safely: {direction}")
            return

        is_closing = "close" in direction_lower or "reduce" in direction_lower
        is_opening = "open" in direction_lower or "add" in direction_lower
        if not (is_opening or is_closing):
            logger.warning(f"Skipping unknown fill direction: {direction}")
            return

        if "long" in direction_lower:
            position_side = PositionSide.LONG
        elif "short" in direction_lower:
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
            follower_balance = await client.get_portfolio_account_value(
                settings.hyperliquid.wallet_address
            )
            if follower_balance is None:
                logger.error("Unable to read follower wallet equity; skipping fill")
                return
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

            target_pre_close_size = float(fill_data.get("_target_pre_close_size", 0.0))
            if (
                target_pre_close_size <= 0
                and target_position is not None
                and target_position.side == position_side
            ):
                target_pre_close_size = target_position.size + target_size

            our_size, close_ratio = calculate_scaled_close_size(
                target_fill_size=target_size,
                target_pre_close_size=target_pre_close_size,
                follower_size=follower_size,
            )
            target_fully_closed = (
                target_pre_close_size > 0
                and target_size >= target_pre_close_size - 1e-12
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
            async with trade_execution_lock:
                if is_stopped:
                    logger.info("Bot stopped while preparing close fill; no exchange request sent")
                    return
                result = await executor.execute_market_order(
                    symbol=symbol,
                    side=order_side,
                    size=Decimal(str(our_size)),
                    reduce_only=True,
                )
        else:
            target_balance = await client.get_portfolio_account_value(settings.target_wallet)
            if settings.copy_rules.auto_adjust_size:
                if target_balance is None or target_balance <= 0:
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
                max_leverage=await executor.get_max_leverage(symbol),
            )
            if settings.simulated_trading:
                follower_position = simulated_positions.get(symbol)
                open_positions = len(simulated_positions)
            else:
                follower_position = next(
                    (position for position in follower_dex_state.positions if position.symbol == symbol), None
                )
                open_positions = len(follower_state.positions)

            max_order_notional = await get_follower_order_notional_cap(
                symbol,
                OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL,
                leverage,
                dex_state=follower_dex_state,
                prepare_leverage=True,
            )
            if max_order_notional is None:
                logger.error(f"Unable to read available order capacity for {symbol}")
                await _notify_copy_failure(
                    symbol=symbol, direction=direction, target_size=target_size,
                    follower_size=our_size, price=price, category="可交易额度查询失败",
                    reason="Unable to read the follower's available order capacity",
                    fill_id=fill_id,
                )
                return
            if max_order_notional <= 0:
                await _notify_copy_failure(
                    symbol=symbol, direction=direction, target_size=target_size,
                    follower_size=our_size, price=price, category="保证金不足",
                    reason=f"Available order capacity for {symbol} is ${max_order_notional:.2f}",
                    fill_id=fill_id,
                )
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

            max_size_from_margin = max_order_notional / price
            existing_position_value = follower_position.notional_value if follower_position else 0.0
            remaining_position_value = remaining_position_budget(existing_position_value)
            max_size_from_position_limit = (
                remaining_position_value / price
                if remaining_position_value is not None else float("inf")
            )
            our_size = min(our_size, max_size_from_margin, max_size_from_position_limit)
            if our_size * price < MIN_POSITION_SIZE_USD:
                logger.warning(
                    f"Skipping {symbol}: copied fill value ${our_size * price:.2f} is below "
                    f"${MIN_POSITION_SIZE_USD:.2f} after margin/position-cap limits"
                )
                await _notify_copy_failure(symbol=symbol, direction=direction, target_size=target_size,
                                           follower_size=our_size, price=price, category="低于最小订单金额",
                                           reason=f"Follower notional ${our_size * price:.2f} is below ${MIN_POSITION_SIZE_USD:.2f} after risk limits",
                                           fill_id=fill_id)
                return

            order_side = OrderSide.BUY if position_side == PositionSide.LONG else OrderSide.SELL
            async with trade_execution_lock:
                if is_stopped:
                    logger.info("Bot stopped while preparing opening fill; no exchange request sent")
                    return
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
        balance = await get_follower_balance() or 0
        pnl = follower_state.unrealized_pnl if follower_state else 0
    
    status_emoji = "🔴" if is_stopped else ("🟢" if not is_paused else "⏸️")
    status_text = "已停止（发 /start 恢复）" if is_stopped else ("运行中" if not is_paused else "已暂停（发 /start 恢复）")
    if wallet_unfunded:
        status_emoji = "💸"
        status_text = "跟单已停止（钱包无资金，入金后发 /start）"
    mode = "模拟" if settings.simulated_trading else "实盘"
    if settings.simulated_trading:
        position_lines = [
            f"• {html.escape(symbol)} {str(position.get('side', 'unknown')).upper()}："
            f"数量 {abs(position.get('size', 0)):.6f}｜未实现盈亏 ${position.get('unrealized_pnl', 0):+,.2f}"
            for symbol, position in simulated_positions.items()
        ]
    else:
        position_lines = [
            f"• {html.escape(position.symbol)} {position.side.value.upper()}："
            f"数量 {position.size:,.6f}｜未实现盈亏 ${position.unrealized_pnl:+,.2f}"
            for position in (follower_state.positions if follower_state else [])
        ]
    position_details = "\n".join(position_lines) if position_lines else "暂无持仓"

    flow_cfg = settings.target_equity_flow
    if not flow_cfg.enabled:
        flow_status = "已关闭"
    elif target_flow_last_equity is None:
        flow_status = f"等待首次采样（每 {flow_cfg.poll_seconds}s）"
    else:
        checked = (
            target_flow_last_check_at.strftime("%H:%M:%S")
            if target_flow_last_check_at
            else "?"
        )
        flow_status = (
            f"目标净值 ${target_flow_last_equity:,.2f}｜采样 {checked}"
            f"｜每 {flow_cfg.poll_seconds}s"
        )

    funding_note = (
        "钱包净值 $0，/orders /copyloss /close 已禁用；入金后发 /start"
        if wallet_unfunded
        else "正常"
    )

    return f"""
📊 <b>跟单运行状态</b>

{status_emoji} <b>状态：</b>{status_text}
🎮 <b>模式：</b>{mode}
👤 <b>目标：</b><code>{settings.target_wallet[:10]}...{settings.target_wallet[-6:]}</code>
💼 <b>账户余额：</b>${balance:,.2f}
📈 <b>未实现盈亏：</b>${pnl:,.2f}
📊 <b>已复制成交：</b>{trades_copied_count}
📍 <b>持仓数：</b>{len(simulated_positions) if settings.simulated_trading else (len(follower_state.positions) if follower_state else 0)}

<b>自己的持仓</b>
{position_details}
⏰ <b>运行时长：</b>{uptime:.1f} 小时

<b>仓位模式：</b>{settings.sizing.mode.title()}
<b>杠杆：</b>目标杠杆的 {settings.leverage.adjustment_ratio} 倍
<b>追入旧仓位：</b>{existing_position_policy_text()}
<b>目标资金监控：</b>{flow_status}
<b>资金限制：</b>{funding_note}
<b>版本：</b>{build_stamp()}
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


def _wallet_full(address: Optional[str]) -> str:
    """Return the complete address for surfaces used to verify what is running.

    ``/pnl`` is where a user checks which wallets are actually involved, so a
    truncated label is useless there — copy the full 42-character address.
    """
    return html.escape(address) if address else "未配置"


async def get_pnl() -> str:
    """Get PnL for Telegram"""
    # Refresh independently of the monitor cache.  The monitor's snapshot is
    # used for event reconciliation and must not be overwritten by a command.
    target_state = (
        await client.get_user_state(settings.target_wallet)
        if client else None
    )
    follower_state = None
    
    if settings.simulated_trading:
        balance = simulated_balance
        equity = simulated_balance
        pnl = simulated_pnl
        mode = "模拟"
    else:
        follower_state = await _get_follower_state()
        equity = await get_follower_balance() or 0
        balance = equity
        pnl = follower_state.unrealized_pnl if follower_state else 0
        mode = "实盘"

    target_performance = await client.get_portfolio_performance(settings.target_wallet) if client else {}
    follower_performance = (
        await client.get_portfolio_performance(settings.hyperliquid.wallet_address)
        if client and not settings.simulated_trading and settings.hyperliquid.wallet_address else {}
    )
    target_line = _wallet_full(settings.target_wallet)
    follower_line = (
        _wallet_full(settings.hyperliquid.wallet_address)
        if not settings.simulated_trading
        else "模拟账户"
    )
    target_equity = (
        await client.get_portfolio_account_value(settings.target_wallet)
        if client and target_state else None
    )
    target_positions = "\n".join(
        f"• {html.escape(position.symbol)} {position.side.value.upper()}："
        f"{position.size:,.6f}｜杠杆 {position.leverage:g}x｜"
        f"入场 ${position.entry_price:,.4f}｜当前 ${position.current_price:,.4f}｜"
        f"未实现盈亏 ${position.unrealized_pnl:+,.2f}"
        for position in target_state.positions
    ) if target_state and target_state.positions else "暂无合约持仓"
    target_summary = (
        f"账户总金额：${target_equity:,.2f}｜Perps 持仓数：{len(target_state.positions)}｜"
        f"未实现盈亏：${target_state.unrealized_pnl:,.2f}"
        if target_equity is not None and target_state
        else "账户净值 / 盈亏数据暂不可用"
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
<b>目标钱包持仓</b>
{target_positions}
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


async def get_wallet_report(address: str, fill_limit: int = 30) -> str:
    """Format public account performance and recent fills for Telegram."""
    if not client:
        return "❌ Hyperliquid 客户端尚未初始化。"

    state = await client.get_user_state(address)
    total_equity = await client.get_portfolio_account_value(address)
    performance = await client.get_portfolio_performance(address)
    fills = await client.get_user_fills(address, fill_limit)
    # Spot fills are labeled "@<pair index>" by the exchange; resolve them to a
    # readable pair name so a dust conversion is not reported as mystery text.
    spot_labels = (
        await client.get_spot_pair_labels()
        if any(str(fill["symbol"]).startswith("@") for fill in fills)
        else {}
    )
    if state and total_equity is not None:
        account_summary = (
            f"• 账户总金额：${total_equity:,.2f}\n"
            f"• 未实现盈亏：${state.unrealized_pnl:,.2f}\n"
            f"• 当前持仓：{len(state.positions)}｜挂单：{len(state.orders)}"
        )
    elif state:
        account_summary = (
            "• 账户总金额暂不可用（portfolio 数据缺失）\n"
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
            symbol_label = spot_labels.get(str(fill["symbol"]))
            display_symbol = f"{symbol_label} 现货" if symbol_label else fill["symbol"]
            lines.append(
                f"<b>{index}. {html.escape(display_symbol)}</b> {html.escape(direction)}\n"
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


async def blocked_command_text() -> Optional[str]:
    """Async wrapper for the Telegram bot's command gate."""
    return unfunded_block_text()


async def refresh_funding_state() -> tuple[bool, str]:
    """Re-read the live wallet's equity and clear the unfunded flag if funded.

    Returns ``(funded, text)`` where ``text`` is the message to show the
    operator: the reason it is still blocked, or a confirmation of the balance
    that was just picked up.
    """
    global wallet_unfunded, simulated_balance

    if settings.simulated_trading or not wallet_unfunded:
        return True, ""

    address = settings.hyperliquid.wallet_address
    if not client or not address:
        return False, "❌ 机器人尚未就绪或未配置跟随钱包，稍后再试。"

    equity = await client.get_portfolio_account_value(address)
    if equity is None:
        return False, "❌ 读取跟随钱包净值失败（网络或接口问题），跟单仍处于停止状态。"
    if equity <= 0:
        return False, (
            "💸 <b>跟随钱包净值仍为 $0.00</b>\n\n"
            f"钱包：<code>{address}</code>\n"
            "跟单保持停止；入金后重新发 /start 即可恢复。"
        )

    simulated_balance = equity
    target_equity = await client.get_portfolio_account_value(settings.target_wallet)
    if target_equity and target_equity > 0:
        # Ratio was 0 while unfunded; recompute from the money that actually
        # arrived so the first copied order is not sized against a stale $0.
        settings.sizing.portfolio_ratio = equity / target_equity
    wallet_unfunded = False
    logger.success(
        f"💰 Follower wallet funded: ${equity:,.2f} — copying enabled again"
    )
    return True, f"💰 检测到跟随钱包资金 <b>${equity:,.2f}</b>\n"


async def handle_start() -> str:
    """/start: start or resume copying.

    Merged from the old /start (help text only) and /resume: a paused bot is
    unpaused, a stopped bot gets a fresh monitor and the monitor loop is woken
    up, so a stop no longer requires a container restart to undo.
    """
    global is_paused, is_stopped, paused_since, monitor

    if not client or not executor:
        return "❌ 机器人尚未就绪，请稍后再试。"

    # A wallet that started at $0 keeps copying stopped until money shows up;
    # /start is the only place that re-checks, so no polling loop is needed.
    funded_note = ""
    if wallet_unfunded:
        funded, funded_note = await refresh_funding_state()
        if not funded:
            return funded_note
        if not is_stopped:
            is_paused = False
            paused_since = None
            logger.warning("▶️ Copying re-enabled after the wallet was funded")
            return funded_note + "已恢复跟单（此前因钱包无资金而停止）。"

    if is_stopped:
        # A fresh monitor gives a clean event baseline, exactly like a target
        # switch: no historical fills are replayed after the gap.
        new_monitor = WalletMonitor(
            settings.target_wallet,
            settings.hyperliquid.api_url,
            settings.hyperliquid.ws_url,
            fill_polling_enabled=settings.copy_rules.fill_polling_enabled,
            fill_poll_interval_seconds=settings.copy_rules.fill_poll_interval_seconds,
        )
        configure_monitor_callbacks(new_monitor)
        monitor = new_monitor
        is_stopped = False
        is_paused = False
        paused_since = None
        restart_requested.set()
        logger.warning("▶️ Bot resumed by /start after being stopped")
        return funded_note + "▶️ 已恢复跟单：已重新连接目标监控，继续复制新成交（仓位保持不变）。"

    if is_paused:
        is_paused = False
        paused_since = None
        logger.info("▶️ Bot resumed by /start")
        return "▶️ 已恢复复制新成交（此前为暂停状态）。"

    return "ℹ️ 机器人本来就在运行中，无需启动。"


async def handle_close(
    selection: Optional[list[str]] = None,
    confirm: bool = False,
) -> str:
    """/close: close the follower's own positions, chosen by number or symbol.

    Manual by design and deliberately independent of the copy state: /stop
    keeps positions on purpose, so closing must work while the bot is paused
    or stopped. Without ``confirm`` it only lists what would be closed, because
    in live mode every item is a real reduce-only market order.
    """
    if not client or not executor:
        return "❌ 机器人尚未就绪，请稍后再试。"
    blocked = unfunded_block_text()
    if blocked:
        return blocked

    # Read the follower's own book, never the target's cached positions.
    if settings.simulated_trading:
        book: list[dict] = []
        for symbol, data in simulated_positions.items():
            size = float(data.get("size", 0.0) or 0.0)
            if size == 0:
                continue
            side = str(data.get("side") or ("LONG" if size > 0 else "SHORT")).upper()
            entry = float(data.get("entry_price", 0.0) or 0.0)
            book.append({
                "symbol": symbol,
                "side": side,
                "size": abs(size),
                "entry_price": entry,
                "unrealized_pnl": 0.0,
            })
    else:
        follower_state = await _get_follower_state()
        if follower_state is None:
            return (
                "❌ 无法读取跟随钱包当前持仓，已取消。\n"
                "请稍后重试；若持续失败，检查 API 连接或钱包地址配置。"
            )
        book = [
            {
                "symbol": position.symbol,
                "side": position.side.value.upper(),
                "size": abs(float(position.size)),
                "entry_price": float(position.entry_price),
                "unrealized_pnl": float(position.unrealized_pnl or 0.0),
            }
            for position in follower_state.positions
            if abs(float(position.size)) > 0
        ]

    if not book:
        return "ℹ️ 跟随账户当前没有任何持仓，无需平仓。"

    # Selection: 1-based numbers into this list, or symbol substrings
    # (case-insensitive, so "btc" matches "BTC" and "skhx" matches "xyz:SKHX").
    chosen: list[dict] = []
    unknown: list[str] = []
    if selection:
        wanted: list[dict] = []
        for token in selection:
            token = token.strip()
            if not token:
                continue
            if token.lower() in ("all", "全部"):
                wanted = list(book)
                break
            if token.isdigit():
                number = int(token)
                if 1 <= number <= len(book):
                    if book[number - 1] not in wanted:
                        wanted.append(book[number - 1])
                else:
                    unknown.append(token)
                continue
            matches = [
                row for row in book
                if token.lower() in row["symbol"].lower()
            ]
            if matches:
                for row in matches:
                    if row not in wanted:
                        wanted.append(row)
            else:
                unknown.append(token)
        if unknown:
            listed = "、".join(html.escape(str(item)) for item in unknown)
            return (
                f"❌ 无法识别的序号或币种：{listed}\n"
                f"当前可选 1-{len(book)}。发送 <code>/close</code> 查看列表。"
            )
        chosen = wanted
    else:
        chosen = list(book)

    lines = [
        f"🔻 <b>平仓{'（预览）' if not confirm else ''}</b>｜"
        f"{'模拟' if settings.simulated_trading else '实盘'}",
        "",
    ]
    selected_ids = {id(row) for row in chosen}
    total_value = 0.0
    for index, row in enumerate(book, 1):
        current_price = await _close_reference_price(row["symbol"], row["entry_price"])
        value = row["size"] * current_price
        picked = " ✅" if id(row) in selected_ids else ""
        total_value += value
        lines.append(
            f"{index}. {html.escape(row['symbol'])} {row['side']}｜"
            f"数量 {row['size']:.6g}｜≈ ${value:,.0f}｜"
            f"PnL ${row['unrealized_pnl']:+,.0f}{picked}"
        )
    if selection and chosen:
        names = " ".join(
            str(next(i for i, row in enumerate(book, 1) if row is item))
            for item in chosen
        )
        lines.append("")
        lines.append(f"已选 {len(chosen)} 笔，合计 ≈ ${total_value:,.2f}。")
        if not confirm:
            lines.append(f"发送 <code>/close {names} confirm</code> 执行平仓。")
    elif not confirm:
        lines.append("")
        lines.append(
            f"全部 {len(book)} 笔合计 ≈ ${total_value:,.2f}。"
            f"全部平仓：<code>/close all confirm</code>；"
            f"只平部分：按序号 <code>/close 1 3 confirm</code> 或按币种 "
            f"<code>/close BTC confirm</code>。"
        )
    if not confirm:
        lines.append("未加 confirm 之前不会发送任何订单。")
        return "\n".join(lines)

    if close_positions_lock.locked():
        return "⏳ 上一次平仓仍在执行，请稍后再试。"

    started_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 5000
    records: list[dict[str, Any]] = []
    async with close_positions_lock:
        for index, row in enumerate(chosen, 1):
            symbol = row["symbol"]
            close_side = OrderSide.SELL if row["side"] == "LONG" else OrderSide.BUY
            try:
                # The reduce-only size and side come from the same read that
                # built the list, but the order itself only goes out under the
                # trade lock, after /stop's cancel path has drained.
                async with trade_execution_lock:
                    order_id = await executor.close_position(
                        symbol,
                        size=Decimal(str(row["size"])),
                        side=close_side,
                    )
            except Exception as exc:
                logger.exception(f"/close failed for {symbol}: {exc}")
                order_id = None
                reason = f"异常：{exc}"
            else:
                reason = (
                    (getattr(executor, "last_error", "") or "").strip()
                    or "交易所未返回订单号"
                ) if not order_id else ""

            record = {
                "symbol": symbol,
                "side": close_side.value.upper(),
                "ok": bool(order_id),
                "size": row["size"],
                "notional": 0.0,
                "order_id": str(order_id) if order_id else None,
                "fill_price": None,
                "fill_size": None,
                "closed_pnl": None,
                "reason": reason,
            }
            current_price = await _close_reference_price(symbol, row["entry_price"])
            record["notional"] = row["size"] * current_price
            records.append(record)
            if not record["ok"]:
                logger.error(f"/close failed for {symbol}: {record['reason']}")

    fills = await _recent_fills_by_symbol(started_at_ms)
    for record in records:
        if not record["ok"]:
            continue
        fill = fills.get(record["symbol"]) or fills.get(record["symbol"].lower())
        if fill:
            record["fill_price"] = fill.get("price")
            record["fill_size"] = fill.get("size")
            record["closed_pnl"] = fill.get("closed_pnl")

    if settings.simulated_trading:
        for record in records:
            if not record["ok"]:
                continue
            row = next((item for item in chosen if item["symbol"] == record["symbol"]), None)
            if row is None:
                continue
            _apply_simulated_close(row, record["fill_price"] or record["notional"] / max(row["size"], 1e-12))

    return _close_receipt_text(records)


def _apply_simulated_close(row: dict, exit_price: float) -> None:
    """Mirror on_position_close's simulator accounting for a manual close."""
    global simulated_balance, simulated_pnl
    symbol = row["symbol"]
    position = simulated_positions.get(symbol)
    if position is None:
        return
    quantity = abs(float(position.get("size", 0.0) or 0.0))
    entry = float(position.get("entry_price", 0.0) or 0.0)
    side = str(position.get("side") or ("LONG" if float(position.get("size", 0.0)) > 0 else "SHORT")).upper()
    if side == "LONG":
        pnl = quantity * (exit_price - entry)
    else:
        pnl = quantity * (entry - exit_price)
    value = float(position.get("value", quantity * entry) or 0.0)
    leverage = float(position.get("leverage", 1) or 1)
    margin_used = value / leverage if leverage > 0 else 0.0
    simulated_balance += margin_used + pnl
    simulated_pnl += pnl
    # /close always closes the whole position, so the book entry goes away.
    del simulated_positions[symbol]
    logger.success(
        f"💰 SIMULATED MANUAL CLOSE: {symbol} exit=${exit_price:,.2f} "
        f"pnl=${pnl:,.2f} balance=${simulated_balance:,.2f}"
    )


async def _close_reference_price(symbol: str, fallback: float) -> float:
    """Best-effort market price for a close preview/receipt."""
    try:
        price = await client.get_market_price(symbol)
        if price and price > 0:
            return float(price)
    except Exception:
        pass
    return float(fallback or 0.0)


def _close_receipt_text(records: list[dict[str, Any]]) -> str:
    """Render the per-position receipt for a manual close run."""
    stamp = datetime.now(SHANGHAI_TZ).strftime("%m-%d %H:%M:%S")
    filled = [record for record in records if record["ok"]]
    total_notional = sum(record["notional"] for record in filled)
    lines = [
        f"🧾 <b>平仓回执</b>｜{'模拟' if settings.simulated_trading else '实盘'}｜{stamp}",
        "",
    ]
    for index, record in enumerate(records, 1):
        symbol = html.escape(str(record["symbol"]))
        side = record["side"]
        if not record["ok"]:
            reason = html.escape(str(record["reason"] or "未知原因"))
            lines.append(f"{index}. ❌ <b>{symbol} {side}</b>")
            lines.append(f"   未平仓：{reason}")
            continue
        size = record["fill_size"] or record["size"]
        price = record["fill_price"]
        if price:
            price_text = f"@ ${price:,.4f}"
        elif settings.simulated_trading:
            price_text = "（模拟，按当时市价）"
        else:
            price_text = "（未取到成交价，以交易所为准）"
        lines.append(f"{index}. ✅ <b>{symbol} {side}</b>｜reduce-only")
        lines.append(
            f"   平掉 {float(size):.6g} {price_text}｜名义 ${record['notional']:,.2f}"
        )
        if record.get("closed_pnl") is not None:
            lines.append(f"   已实现盈亏 ${record['closed_pnl']:+,.2f}")
        if record["order_id"]:
            lines.append(f"   订单 <code>{html.escape(str(record['order_id']))}</code>")
    lines.append("")
    lines.append(
        f"合计：成功 {len(filled)}/{len(records)}｜平仓名义 ${total_notional:,.2f}"
    )
    remaining = len(simulated_positions) if settings.simulated_trading else None
    if remaining is not None:
        lines.append(f"模拟账户剩余持仓：{remaining} 个。")
    return "\n".join(lines)


async def handle_follow(address: str) -> str:
    """Switch the monitored target wallet without restarting the process."""
    global monitor

    normalized = str(address or "").strip().lower()
    if not re.fullmatch(r"0x[a-f0-9]{40}", normalized):
        return "❌ 地址格式无效：请输入 0x 开头的 40 位十六进制钱包地址。"
    if is_stopped:
        return "🔴 机器人已停止，请先手动重启容器后再更换跟单地址。"
    if normalized == str(settings.target_wallet).lower():
        return f"ℹ️ 当前已经在跟随：<code>{html.escape(_wallet_label(normalized))}</code>"
    if not client:
        return "❌ Hyperliquid 客户端尚未就绪。"

    async with target_switch_lock:
        if not await client.validate_wallet_address(normalized):
            return "❌ 地址不存在、不可查询或暂时无法连接 Hyperliquid。"
        try:
            persist_target_wallet(normalized)
        except Exception as exc:
            logger.error(f"Unable to persist target wallet in .env: {exc}")
            return "❌ 地址验证通过，但写入 .env 失败，跟单地址未切换。"

        logger.warning(
            f"🔄 Switching follow target: {settings.target_wallet} -> {normalized}"
        )
        old_monitor = monitor
        new_monitor = WalletMonitor(
            normalized,
            settings.hyperliquid.api_url,
            settings.hyperliquid.ws_url,
            fill_polling_enabled=settings.copy_rules.fill_polling_enabled,
            fill_poll_interval_seconds=settings.copy_rules.fill_poll_interval_seconds,
        )
        configure_monitor_callbacks(new_monitor)
        # Publish the replacement before closing the old websocket. The main
        # monitor loop can then never mistake the old instance for the active
        # target when its listener returns after the close.
        settings.target_wallet = normalized
        monitor = new_monitor

        # Stop the old websocket and its fill polling before replacing the
        # monitor's active event stream.
        if old_monitor:
            await old_monitor.stop_monitoring()

        # Remove only mirror orders owned by this process. Do not cancel the
        # follower's unrelated manual orders.
        seen_mirrors: set[int] = set()
        for mirror in list(mirrored_orders.values()):
            if id(mirror) in seen_mirrors or not mirror.get("active"):
                continue
            seen_mirrors.add(id(mirror))
            try:
                await executor.cancel_order(mirror["symbol"], mirror["follower_oid"])
            except Exception as exc:
                logger.warning(f"Unable to cancel old mirror order: {exc}")
        mirrored_orders.clear()
        pending_order_batches.clear()
        for task in pending_order_tasks.values():
            if not task.done():
                task.cancel()
        pending_order_tasks.clear()
        pending_target_order_ids.clear()
        pending_target_fills.clear()
        processed_fill_ids.clear()
        counted_mirror_order_ids.clear()

    return (
        "✅ <b>跟单地址已切换</b>\n\n"
        f"目标钱包：<code>{html.escape(_wallet_label(normalized))}</code>\n"
        "已建立新地址基线，不会补跟此前历史成交。"
    )


async def handle_stop():
    """Handle stop request from Telegram: stop copying, keep positions.

    The old /stop asked whether to close everything; that choice now lives in
    /close, which can also pick single symbols. Positions are deliberately
    kept here — stopping should never liquidate the account as a side effect.
    """
    global is_paused, is_stopped, paused_since
    logger.warning("🛑 Stop requested from Telegram (positions are kept)")
    is_paused = True
    is_stopped = True
    if paused_since is None:
        paused_since = datetime.now(timezone.utc)

    # Do not let an order batch scheduled before /stop place a follower order
    # after monitoring has been shut down.
    for task in pending_order_tasks.values():
        if not task.done():
            task.cancel()
    pending_order_tasks.clear()
    pending_order_batches.clear()
    pending_target_order_ids.clear()
    pending_target_fills.clear()

    # Wait for an already-started copy action before cancelling orders. Once
    # this lock is held, no earlier mirror coroutine can submit after /stop.
    async with trade_execution_lock:
        if executor:
            await executor.cancel_all_orders()

    # Stop monitoring
    if monitor:
        await monitor.stop_monitoring()

    # Keep Telegram alive to report the stopped status and to accept /start.
    logger.warning("🛑 Bot stopped. Send /start to resume copying.")


def _target_equity_snapshot(wallet: str, state, captured_at_ms: int) -> EquitySnapshot:
    """Turn one target read into a comparable snapshot.

    Equity, unrealized PnL and sizes all come from the same ``get_user_state``
    response. Mixing a cached portfolio value with a fresh position read would
    create a phantom "deposit" whenever the two were sampled at different
    prices.
    """
    signed_positions = tuple(
        (
            position.symbol,
            float(position.size)
            if str(position.side.value).lower() == "long"
            else -float(position.size),
        )
        for position in state.positions
    )
    return EquitySnapshot(
        wallet=wallet,
        equity=float(state.balance),
        unrealized_pnl=float(state.unrealized_pnl),
        positions=signed_positions,
        captured_at_ms=captured_at_ms,
    )


async def check_target_fund_flow_once() -> None:
    """One poll of the target wallet: build/compare a snapshot, alert if needed.

    Split out of the loop so the whole decision path can be exercised with a
    stub client. A bad read always returns without touching the baseline,
    because a partial DEX aggregate looks exactly like a large withdrawal.
    """
    global last_target_equity_snapshot, target_flow_next_alert_at
    global target_flow_last_check_at, target_flow_last_equity

    cfg = settings.target_equity_flow
    if not client:
        return
    wallet = settings.target_wallet
    state = await client.get_user_state(wallet)
    if state is None or state.balance <= 0:
        logger.warning("💰 Target fund-flow poll skipped: account state unavailable")
        return

    now_ms = int(time.time() * 1000)
    snapshot = _target_equity_snapshot(wallet, state, now_ms)
    target_flow_last_check_at = datetime.now(SHANGHAI_TZ)
    target_flow_last_equity = snapshot.equity
    previous = last_target_equity_snapshot
    if previous is None or previous.wallet.lower() != wallet.lower():
        last_target_equity_snapshot = snapshot
        logger.info(
            f"💰 Target fund-flow baseline: {snapshot.wallet[:10]}... "
            f"equity=${snapshot.equity:,.2f} positions={len(snapshot.positions)}"
        )
        return

    flow = analyze_equity_flow(
        previous,
        snapshot,
        min_usd=cfg.min_usd,
        min_pct=cfg.min_pct,
        max_position_change_pct=cfg.max_position_change_pct,
    )
    # Always advance the baseline, alert or not: the same transfer must never
    # be reported twice.
    last_target_equity_snapshot = snapshot
    if flow is None:
        return
    if time.time() < target_flow_next_alert_at:
        logger.info(
            f"💰 Target fund-flow alert suppressed (cooldown): "
            f"{flow.direction} ${flow.amount:,.2f}"
        )
        return

    # A little slack on both ends: the ledger index can lag a fresh transfer,
    # and the previous poll's wall clock is only an estimate of when it happened.
    window_start = previous.captured_at_ms - 60_000
    window_end = now_ms + 60_000
    try:
        updates = await client.get_non_funding_ledger_updates(
            wallet, window_start, window_end
        )
        ledger = summarize_ledger_updates(
            updates, wallet, start_ms=window_start, end_ms=window_end
        )
    except Exception as exc:
        logger.warning(f"Target ledger lookup failed: {exc}")
        ledger = summarize_ledger_updates(
            [],
            wallet,
            start_ms=window_start,
            end_ms=window_end,
            ok=False,
            error=str(exc)[:200],
        )

    # A target that moves money between its own spot and perp books shows up
    # as an equity jump while no cash entered or left. Reporting that would be
    # a false alarm: the follower's copy ratio is unaffected in the long run.
    if ledger.explains_internal(flow.net_flow):
        logger.info(
            f"💰 Target equity move is an internal book transfer "
            f"(equity {flow.net_flow:+,.2f} vs internal {ledger.internal_net:+,.2f}, "
            f"{ledger.internal_count} legs) — no alert"
        )
        return

    portfolio_equity = await client.get_portfolio_account_value(wallet)
    logger.warning(
        f"💰 Target fund flow detected: {wallet} {flow.direction} "
        f"${flow.amount:,.2f} equity ${flow.equity_before:,.2f} -> "
        f"${flow.equity_after:,.2f}, positions changed "
        f"{flow.position_change_pct:.2f}%, ledger={ledger_lines(ledger)}"
    )
    if notifier:
        await notifier.send_target_fund_flow_notification(
            wallet=wallet,
            direction=flow.direction,
            net_flow=flow.net_flow,
            equity_before=flow.equity_before,
            equity_after=flow.equity_after,
            equity_delta=flow.equity_delta,
            unrealized_pnl_delta=flow.unrealized_pnl_delta,
            flow_pct=flow.flow_pct,
            position_change_pct=flow.position_change_pct,
            ledger_text=ledger_lines(ledger),
            chain_net=ledger.chain_net if ledger.matched else None,
            chain_matched=ledger.matched,
            portfolio_equity=portfolio_equity,
        )
    target_flow_next_alert_at = time.time() + cfg.cooldown_seconds


async def poll_target_fund_flow():
    """Watch the target wallet for deposits/withdrawals and alert on Telegram.

    Runs independently of the copy loop: it is read-only, so it keeps reporting
    even after /stop, and a /follow switch simply resets the comparison basis
    on the next poll instead of requiring explicit coordination.
    """
    cfg = settings.target_equity_flow
    if not cfg.enabled:
        logger.info("💰 Target fund-flow monitoring is disabled")
        return
    logger.info(
        f"💰 Target fund-flow monitor started: every {cfg.poll_seconds}s, "
        f"alert above ${cfg.min_usd:,.0f} / {cfg.min_pct:g}% with positions "
        f"changing under {cfg.max_position_change_pct:g}%"
    )
    while True:
        await asyncio.sleep(cfg.poll_seconds)
        try:
            await check_target_fund_flow_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Target fund-flow poll failed: {exc}")


async def send_hourly_reports():
    """Send hourly reports via Telegram"""
    while True:
        try:
            await asyncio.sleep(3600)  # Wait 1 hour
            
            if not is_stopped and notifier and monitor and monitor.current_state:
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
                    account_balance = await get_follower_balance()
                    if account_balance is None:
                        continue
                    open_positions = len(state.positions)
                    open_orders = len(state.orders)
                
                await notifier.send_hourly_report(
                    trades_copied=trades_copied_count,
                    account_pnl_usd=account_pnl,
                    account_pnl_pct=(account_pnl / account_balance * 100) if account_balance > 0 else 0,
                    open_positions=open_positions,
                    open_orders=open_orders,
                    target_wallet=settings.target_wallet,
                    copy_status=copying_status(),
                )
        except Exception as e:
            logger.error(f"Error sending hourly report: {e}")


async def run_target_monitor():
    """Keep monitoring the current target and support hot target switches."""
    global monitor
    while True:
        current_monitor = monitor
        await current_monitor.start_monitoring()
        if is_stopped:
            # /stop deliberately keeps Telegram alive, but this task must not
            # reconnect or consume new target events afterward. /start sets
            # restart_requested to resume with a fresh monitor, so no container
            # restart is needed to undo a stop.
            await restart_requested.wait()
            restart_requested.clear()
        if monitor is current_monitor:
            logger.warning("Target monitor stopped unexpectedly; reconnecting")
            await asyncio.sleep(1)

async def main():
    """
    Main entry point for the copy trading bot
    """
    global monitor, executor, position_sizer, client, telegram_bot, notifier, bot_start_time
    global simulated_balance, trades_copied_count, counted_mirror_order_ids, is_stopped
    global is_paused, paused_since, wallet_unfunded
    
    bot_start_time = datetime.now(SHANGHAI_TZ)
    trades_copied_count = 0
    is_stopped = False
    wallet_unfunded = False
    is_paused = False
    paused_since = None
    counted_mirror_order_ids.clear()
    pending_order_batches.clear()
    pending_order_tasks.clear()
    pending_target_order_ids.clear()
    pending_target_fills.clear()
    
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
    logger.info(f"🧩 Build: {build_stamp()}")
    
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
    startup_positions: list = []
    
    if state:
        target_balance = await client.get_portfolio_account_value(target_address)
        if target_balance is None:
            raise RuntimeError("Unable to read total target wallet equity")
        logger.info("")
        logger.info(f"💼 Target Account:")
        logger.info(f"   Account Equity: ${target_balance:,.2f}")
        logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
        logger.info(f"   Open Positions: {len(state.positions)}")

        # Existing-position copy scope. ``all`` keeps the historical behaviour;
        # ``loss`` only mirrors positions the target is currently underwater on,
        # so the follower enters at a better price than the target did.
        startup_pnl_filter = settings.copy_rules.copy_open_pnl_filter
        startup_positions = select_existing_positions(
            state.positions, startup_pnl_filter
        )
        if startup_pnl_filter != "all":
            logger.info(
                f"   PnL Filter: {startup_pnl_filter} -> copying "
                f"{len(startup_positions)}/{len(state.positions)} position(s)"
            )
        
        # In live mode, size against the follower wallet's actual balance.
        if not settings.simulated_trading:
            if not settings.hyperliquid.wallet_address or not settings.hyperliquid.private_key:
                raise RuntimeError("Live trading requires HYPERLIQUID_WALLET_ADDRESS and HYPERLIQUID_PRIVATE_KEY")
            follower_total_equity = await client.get_portfolio_account_value(settings.hyperliquid.wallet_address)
            if follower_total_equity is None:
                raise RuntimeError("Unable to read follower wallet total equity")
            simulated_balance = follower_total_equity

            if simulated_balance <= 0:
                # Zero equity in live mode: stop copying instead of exiting.
                # Exiting is worse in every way under `restart: unless-stopped`
                # (a restart every few seconds, and Telegram has not started
                # yet, so the reason never reaches the phone). The bot stays
                # up, reports why, and refuses the three commands that would
                # need money; /start re-checks the balance.
                wallet_unfunded = True
                is_paused = True
                paused_since = datetime.now(timezone.utc)
                logger.error(
                    f"💸 Follower wallet {settings.hyperliquid.wallet_address} has "
                    f"${simulated_balance:,.2f} total equity — copying is STOPPED."
                )
                logger.error(
                    "   /orders /copyloss /close are disabled until funded; "
                    "send /start after depositing. Other commands keep working."
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
        if startup_positions:
            # Find smallest target position value
            smallest_target_value = min(abs(pos.size) * pos.entry_price for pos in startup_positions)
            # Calculate minimum balance needed to copy at $10
            min_balance_needed = MIN_POSITION_SIZE_USD * (target_balance / smallest_target_value)
            
            logger.info("")
            logger.info(f"⚠️  MINIMUM BALANCE CHECK:")
            logger.info(f"   Hyperliquid Min Order Size: ${MIN_POSITION_SIZE_USD:.2f}")
            logger.info(f"   Smallest Target Position: ${smallest_target_value:,.2f}")
            logger.info(f"   Min Balance Needed (for this ratio): ${min_balance_needed:,.2f}")
            
            if simulated_balance < min_balance_needed:
                positions_below_min = sum(1 for pos in startup_positions 
                                         if (abs(pos.size) * pos.entry_price * auto_ratio) < MIN_POSITION_SIZE_USD)
                logger.warning(f"   ⚠️  WARNING: Your balance ${simulated_balance:,.2f} is below recommended minimum!")
                logger.warning(f"   {positions_below_min} out of {len(startup_positions)} positions will be SKIPPED (below $10)")
                logger.warning(f"   Consider increasing balance to ${min_balance_needed:,.2f} to copy all positions")
            else:
                logger.success(f"   ✅ Your balance is sufficient to copy all positions!")
        
        if startup_positions:
            logger.info("")
            logger.info(f"📊 Current Positions:")
            logger.info(f"=" * 60)
            
            total_simulated_margin = 0
            for i, pos in enumerate(startup_positions, 1):
                target_position_value = abs(pos.size) * pos.entry_price
                your_position_value = target_position_value * auto_ratio
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                your_leverage = calculate_adjusted_leverage(
                    target_leverage=pos.leverage,
                    adjustment_ratio=settings.leverage.adjustment_ratio,
                    symbol=pos.symbol,
                    max_leverage=await executor.get_max_leverage(pos.symbol),
                )
                # Estimate startup capacity using the same account-mode-aware
                # per-market limit used by live order paths.
                follower_dex_state = None
                if not settings.simulated_trading:
                    current_follower_state = await client.get_user_state(
                        settings.hyperliquid.wallet_address,
                        dex=perp_dex_for_symbol(pos.symbol),
                    )
                    if current_follower_state is None:
                        logger.error(f"   ❌ Cannot read follower wallet before copying {pos.symbol}")
                        continue
                    follower_dex_state = current_follower_state

                order_side = (
                    OrderSide.BUY if pos.side == PositionSide.LONG else OrderSide.SELL
                )
                margin_limited_value = await get_follower_order_notional_cap(
                    pos.symbol,
                    order_side,
                    your_leverage,
                    dex_state=follower_dex_state,
                )
                if margin_limited_value is None:
                    logger.error(f"   ❌ Cannot read available order capacity for {pos.symbol}")
                    continue
                capped_position_value = cap_position_value(
                    target_position_value * auto_ratio,
                    margin_limited_value,
                    settings.sizing.max_position_size,
                )
                if capped_position_value < MIN_POSITION_SIZE_USD:
                    logger.warning(
                        f"⚠️  Skipping Position {i}/{len(startup_positions)}: {pos.symbol}; "
                        f"allowed value ${capped_position_value:.2f} is below ${MIN_POSITION_SIZE_USD:.2f}"
                    )
                    continue
                if capped_position_value < target_position_value * auto_ratio:
                    logger.warning(
                        f"⚠️  Capping {pos.symbol} startup copy from "
                        f"${target_position_value * auto_ratio:,.2f} to ${capped_position_value:,.2f} "
                        f"(available-margin or position-cap limit)"
                    )
                your_position_value = capped_position_value
                your_size = your_position_value / pos.entry_price if pos.entry_price > 0 else 0
                margin_needed = your_position_value / your_leverage
                total_simulated_margin += margin_needed
                
                logger.info("")
                logger.info(f"   Position {i}: {pos.symbol} {pos.side.value.upper()}")
                logger.info(f"   Target: {pos.size:.4f} @ ${pos.entry_price:,.2f} ({pos.leverage}x)")
                logger.info(f"   Target Value: ${target_position_value:,.2f}")
                logger.info(f"   Target Unrealized PnL: ${position_unrealized_pnl(pos):,.2f}")
                logger.success(f"   → Your Copy: {your_size:.4f} @ ${pos.entry_price:,.2f} ({your_leverage}x)")
                logger.success(f"   → Your Value: ${your_position_value:,.2f}")
                logger.success(f"   → Margin Needed: ${margin_needed:,.2f}")
            
            logger.info("")
            logger.info("=" * 60)
            scope_note = (
                "" if startup_pnl_filter == "all"
                else f" (PnL filter: {startup_pnl_filter},"
                     f" {len(startup_positions)}/{len(state.positions)} selected)"
            )
            logger.warning(f"📊 If you copied all {len(startup_positions)} positions{scope_note}:")
            logger.warning(f"   Total Margin Needed: ${total_simulated_margin:,.2f}")
            logger.warning(f"   Your Balance: ${simulated_balance:,.2f}")
            logger.warning(f"   Remaining: ${simulated_balance - total_simulated_margin:,.2f}")
            logger.info(f"=" * 60)
    
    logger.info(f"")
    logger.info(f"🔧 Copy Trading Settings:")
    logger.info(f"   Sizing Mode: {settings.sizing.mode}")
    logger.info(f"   Leverage Adjustment: {settings.leverage.adjustment_ratio}x")
    logger.info(f"   Max Position Size: {_format_limit(settings.sizing.max_position_size)}")
    logger.info(f"   Max Total Exposure: {_format_limit(settings.sizing.max_total_exposure)}")
    logger.info(f"   Follower Account Mode: {settings.hyperliquid.account_mode}")
    logger.info(
        f"   Max Margin Usage: {settings.copy_rules.max_margin_usage_ratio:.0%}"
    )
    logger.info(
        f"   Fill Polling Fallback: "
        f"{'enabled (' + str(settings.copy_rules.fill_poll_interval_seconds) + 's)' if settings.copy_rules.fill_polling_enabled else 'disabled'}"
    )
    logger.info(f"   Pending Order Mirror: {settings.copy_rules.mirror_pending_orders}")
    logger.info(
        f"   Pending Order Merge Window: "
        f"{settings.copy_rules.pending_order_merge_window_seconds:g}s"
    )
    logger.info(f"   Cancel Mirrored Orders: {settings.copy_rules.cancel_mirrored_orders}")
    logger.info("   Copy Existing Positions: manual — /copyloss only (startup never orders)")
    logger.info(
        f"   Existing Position PnL Filter: {settings.copy_rules.copy_open_pnl_filter}"
    )
    
    position_sizer = PositionSizer(
        mode=settings.sizing.mode,
        fixed_size=settings.sizing.fixed_size,
        portfolio_ratio=settings.sizing.portfolio_ratio,
        max_position_size=settings.sizing.max_position_size,
        max_total_exposure=settings.sizing.max_total_exposure
    )
    
    configure_monitor_callbacks(monitor)
    
    # The target's existing book is never mirrored automatically. This block
    # used to place real market orders before the user could see them (and
    # without a notification), which is exactly what /copyloss now does on
    # demand: numbered list, explicit selection, per-position receipt.
    if settings.copy_rules.copy_open_positions:
        logger.warning(
            "⚠️  COPY_OPEN_POSITIONS is deprecated and no longer places orders "
            "at startup. Use /copyloss in Telegram to pick positions."
        )
    if startup_positions:
        logger.info("")
        logger.info(
            f"🖐  {len(startup_positions)} existing position(s) will NOT be copied "
            f"automatically — send /copyloss in Telegram to choose."
        )
    
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
        telegram_bot.get_orders_callback = get_orders
        telegram_bot.get_pnl_callback = get_pnl
        telegram_bot.get_leaderboard_callback = get_leaderboard
        telegram_bot.get_wallet_callback = get_wallet_report
        telegram_bot.on_start_requested = handle_start
        telegram_bot.on_stop_requested = handle_stop
        telegram_bot.on_close_positions_requested = handle_close
        telegram_bot.on_follow_requested = handle_follow
        telegram_bot.on_copy_existing_requested = handle_copy_existing
        telegram_bot.blocked_commands_callback = blocked_command_text
        
        # Start Telegram bot
        await telegram_bot.start()
        
        # Start hourly reports task
        asyncio.create_task(send_hourly_reports())
        
        logger.info("✅ Telegram bot ready!")
    else:
        logger.warning("⚠️ Telegram bot not configured (add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env)")

    # Read-only monitoring, deliberately started even without Telegram and even
    # after /stop: a target withdrawal matters regardless of the copy state.
    asyncio.create_task(poll_target_fund_flow())

    try:
        # Get initial state
        logger.info("")
        logger.info(f"📊 Fetching initial state...")
        state = await monitor.get_current_state()
        
        if state:
            target_total_equity = await client.get_portfolio_account_value(target_address)
            logger.info("")
            logger.info(f"💼 Target Account:")
            logger.info(
                f"   Account Equity: ${target_total_equity:,.2f}"
                if target_total_equity is not None
                else "   Account Equity: unavailable"
            )
            logger.info(f"   Unrealized PnL: ${state.unrealized_pnl:,.2f}")
            logger.info(f"   Open Positions: {len(state.positions)}")
            if settings.copy_rules.copy_open_pnl_filter != "all":
                logger.info(
                    f"   Existing Position PnL Filter: "
                    f"{settings.copy_rules.copy_open_pnl_filter}"
                )
            
            if state.positions:
                logger.info("")
                logger.info(f" Current Positions:")
                for i, pos in enumerate(state.positions, 1):
                    logger.info(
                        f"   {i}. {pos.symbol} {pos.side.value.upper()}: {pos.size} "
                        f"@ ${pos.entry_price:,.2f} ({pos.leverage}x) "
                        f"PnL ${position_unrealized_pnl(pos):,.2f}"
                    )
        
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
        logger.info("   Copy Existing Positions: manual — /copyloss only (startup never orders)")
        logger.info(
            f"   Existing Position PnL Filter: {settings.copy_rules.copy_open_pnl_filter}"
        )
        logger.info(f"   Copy Existing Orders: {settings.copy_rules.copy_existing_orders}")
        logger.info(f"   Mirror Pending Orders: {settings.copy_rules.mirror_pending_orders}")
        logger.info(
            f"   Pending Order Merge Window: "
            f"{settings.copy_rules.pending_order_merge_window_seconds:g}s"
        )
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
                # A zero ratio is legitimate now (unfunded wallet), and
                # 1/0 here used to crash the whole startup.
                ratio=(
                    f"1:{int(1 / settings.sizing.portfolio_ratio)}"
                    if settings.sizing.portfolio_ratio > 0
                    else "0（钱包无资金）"
                ),
                leverage_adjustment=settings.leverage.adjustment_ratio,
                existing_position_policy=existing_position_policy_text(),
                funding_warning=(
                    f"跟随钱包 <code>{settings.hyperliquid.wallet_address}</code> "
                    "净值 $0.00，不会复制任何成交。"
                    if wallet_unfunded
                    else ""
                ),
            )
        
        # Start monitoring
        await run_target_monitor()
        
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

        # Without this every fatal path leaves an aiohttp session open and the
        # logs end with "Unclosed client session" right after the real error.
        if client:
            await client.close()
        
        logger.info("👋 Bot stopped gracefully")

if __name__ == "__main__":
    asyncio.run(main())
