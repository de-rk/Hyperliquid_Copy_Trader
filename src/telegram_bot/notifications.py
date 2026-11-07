import asyncio
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Bot
from loguru import logger


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


class NotificationService:
    """
    Service for sending Telegram notifications
    """
    
    def __init__(self, bot_token: str, chat_id: str):
        """
        Initialize notification service
        
        Args:
            bot_token: Telegram bot token
            chat_id: Chat ID to send notifications to
        """
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id
        self.enabled = True
        
        logger.info(f"Notification service initialized for chat {chat_id}")
    
    async def send_message(self, message: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured chat"""
        if not self.enabled:
            logger.debug("Notifications disabled, skipping message")
            return False
        
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=parse_mode
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return False
    
    async def send_trade_notification(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        leverage: float,
        target_size: float,
        is_simulated: bool = True
    ):
        """Legacy trade notification; disabled to avoid misleading close-side labels."""
        logger.debug("Trade notifications disabled")
        return False
        
        mode_emoji = "🧪" if is_simulated else "✅"
        mode_text = "[模拟]" if is_simulated else "[实盘]"
        
        message = f"""
{mode_emoji} <b>已复制新成交</b> {mode_text}

<b>币种：</b>{symbol}
<b>方向：</b>{side.upper()}
<b>跟随数量：</b>{size:.4f}
<b>成交价：</b>${entry_price:,.2f}
<b>杠杆：</b>{leverage}x
<b>名义价值：</b>${size * entry_price:,.2f}

━━━━━━━━━━━━━━━━━━
<b>目标成交数量：</b>{target_size:.4f}
<b>时间：</b>{_now_shanghai().strftime('%H:%M:%S UTC+8')}
"""
        sent = await self.send_message(message.strip())
        if not sent:
            logger.error(
                f"Trade notification was not delivered: {symbol} "
                f"{side.upper()} size={size:.8f}"
            )
        return sent

    async def send_order_detected_notification(
        self,
        symbol: str,
        side: str,
        size: float,
        entry_price: float,
        leverage: float,
        target_size: float,
        status: str = "PENDING",
        target_leverage: float | None = None,
        target_oid: str = "",
        follower_oid: str = "",
    ) -> bool:
        """Notify about a mirrored target order before it is filled."""
        leverage_text = f"{leverage:g}x"
        target_leverage_text = (
            f"{target_leverage:g}x" if target_leverage is not None else leverage_text
        )
        # Target orders often cluster: a cancel-and-replace at a new price or a
        # second add looks almost identical to the previous notification. The
        # order ids are the only fields that prove which target order this is.
        oid_lines = ""
        if target_oid:
            oid_lines += f"\n<b>目标订单号：</b><code>{target_oid}</code>"
        if follower_oid:
            oid_lines += f"\n<b>跟随订单号：</b><code>{follower_oid}</code>"
        message = f"""
🧪 <b>检测到新镜像订单</b> [{status}]

<b>币种：</b>{symbol}
<b>方向：</b>{side.upper()}
<b>跟随数量：</b>{size:.4f}
<b>挂单价格：</b>${entry_price:,.2f}
<b>目标杠杆：</b>{target_leverage_text}
<b>跟随杠杆：</b>{leverage_text}
<b>名义价值：</b>${size * entry_price:,.2f}
{oid_lines}

━━━━━━━━━━━━━━━━━━
<b>目标数量：</b>{target_size:.4f}
<b>时间：</b>{_now_shanghai().strftime('%H:%M:%S UTC+8')}
"""
        return await self.send_message(message.strip())

    async def send_copy_failure_notification(
        self,
        symbol: str,
        side: str,
        target_size: float,
        follower_size: float,
        price: float,
        category: str,
        reason: str,
        fill_id: str = "",
        stage: str = "成交跟单",
    ) -> bool:
        """Report a mirror or fill-copy skip/rejection with diagnostic context."""
        notional = follower_size * price
        message = f"""
⚠️ <b>{stage}失败 / 已跳过</b>

<b>币种：</b>{symbol}
<b>方向：</b>{side.upper()}
<b>目标数量：</b>{target_size:.6f}
<b>预计跟随数量：</b>{follower_size:.6f}
<b>预计名义价值：</b>${notional:,.2f}
<b>分类：</b>{category}
<b>原因：</b><code>{reason[:900]}</code>
{f'<b>Fill ID：</b><code>{fill_id}</code>' if fill_id else ''}

<b>时间：</b>{_now_shanghai().strftime('%H:%M:%S UTC+8')}
"""
        return await self.send_message(message.strip())
    
    async def send_target_fund_flow_notification(
        self,
        *,
        wallet: str,
        direction: str,
        net_flow: float,
        equity_before: float | None = None,
        equity_after: float | None = None,
        equity_delta: float | None = None,
        unrealized_pnl_delta: float | None = None,
        flow_pct: float | None = None,
        position_change_pct: float | None = None,
        ledger_text: str = "",
        chain_net: float | None = None,
        chain_matched: bool = False,
        portfolio_equity: float | None = None,
        source: str = "inferred",
    ) -> bool:
        """Alert that the followed wallet deposited or withdrew funds.

        Two shapes: ``source="ledger"`` announces an amount the exchange ledger
        confirmed (this is the only one that also sees cash held in spot), and
        ``source="inferred"`` announces an equity residual when the ledger has
        nothing to show yet. The ledger breakdown is always displayed so a data
        glitch can never be mistaken for a confirmed deposit.
        """
        inflow = direction == "in"
        emoji = "📥" if inflow else "📤"
        label = "转入 / 入金" if inflow else "转出 / 出金"
        short_wallet = (
            f"{wallet[:10]}...{wallet[-6:]}" if len(wallet) > 20 else wallet
        )
        chain_line = ""
        if chain_net is not None and chain_matched:
            gap = net_flow - chain_net
            chain_line = (
                f"\n<b>链上充提净额：</b>{chain_net:+,.2f}"
                f"\n<b>与推断差额：</b>${gap:+,.2f}（浮盈、资金费或内部划转）"
            )
        # A dust-level portfolio value (a wallet emptied into spot) would read
        # as "$0.00" and contradict the flow being announced, so it is hidden.
        portfolio_line = (
            f"\n<b>全账户净值：</b>${portfolio_equity:,.2f}（portfolio 口径，含现货）"
            if portfolio_equity is not None and portfolio_equity >= 1.0
            else ""
        )
        if source == "ledger":
            message = f"""
{emoji} <b>目标地址资金异动 · {label}</b>

👤 <b>目标：</b><code>{short_wallet}</code>
💵 <b>交易所流水净额：</b><b>${abs(net_flow):,.2f}</b>{f"（{flow_pct:+.2f}%）" if flow_pct is not None else ""}{portfolio_line}

━━━━━━━━━━━━━━━━━━
🔗 <b>流水明细：</b>{ledger_text}

⚠️ 这次变动已由交易所流水确认，跟单金额按目标净值比例计算，会自动放大或缩小。
🕐 <b>时间：</b>{_now_shanghai().strftime('%Y-%m-%d %H:%M:%S UTC+8')}
"""
            return await self.send_message(message.strip())

        message = f"""
{emoji} <b>目标地址资金异动 · {label}（净值推断）</b>

👤 <b>目标：</b><code>{short_wallet}</code>
💼 <b>账户净值：</b>${equity_before:,.2f} → ${equity_after:,.2f}（{equity_delta:+,.2f}）{portfolio_line}
📈 <b>浮动盈亏变化：</b>{unrealized_pnl_delta:+,.2f}
🧮 <b>推断净{'转入' if inflow else '转出'}：</b><b>${abs(net_flow):,.2f}</b>（{flow_pct:+.2f}%）
📍 <b>仓位变化：</b>{position_change_pct:.2f}%（判定：基本未变）{chain_line}

━━━━━━━━━━━━━━━━━━
🔗 <b>交易所流水：</b>{ledger_text}

⚠️ 跟单金额按目标账户净值比例计算，会随这次变动自动放大或缩小。
🕐 <b>时间：</b>{_now_shanghai().strftime('%Y-%m-%d %H:%M:%S UTC+8')}
"""
        return await self.send_message(message.strip())

    async def send_position_close_notification(
        self,
        symbol: str,
        pnl: Optional[float] = None,
        is_simulated: bool = True
    ):
        """Send notification about a closed position"""
        
        mode_emoji = "🧪" if is_simulated else "🔴"
        mode_text = "[模拟]" if is_simulated else "[实盘]"
        
        pnl_text = ""
        if pnl is not None:
            pnl_emoji = "📈" if pnl > 0 else "📉"
            pnl_text = f"\n<b>盈亏：</b>{pnl_emoji} ${pnl:,.2f}"
        
        message = f"""
{mode_emoji} <b>仓位已平</b> {mode_text}

<b>币种：</b>{symbol}{pnl_text}
<b>时间：</b>{_now_shanghai().strftime('%H:%M:%S UTC+8')}
"""
        await self.send_message(message.strip())
    
    async def send_hourly_report(
        self,
        trades_copied: int,
        account_pnl_usd: float,
        account_pnl_pct: float,
        open_positions: int,
        open_orders: int,
        target_wallet: str,
        copy_status: str = ""
    ):
        """Send hourly trading report"""
        
        pnl_emoji = "📈" if account_pnl_usd > 0 else "📉"
        status_line = f"🤖 <b>跟单状态：</b>{copy_status}\n" if copy_status else ""
        
        message = f"""
📊 <b>每小时跟单报告</b>

<b>目标：</b><code>{target_wallet[:10]}...{target_wallet[-6:]}</code>

━━━━━━━━━━━━━━━━━━━━━━━━━
{status_line}📈 <b>已复制成交：</b>{trades_copied}
💰 <b>账户未实现盈亏：</b>{pnl_emoji} ${account_pnl_usd:,.2f} ({account_pnl_pct:+.2f}%)
📍 <b>持仓数：</b>{open_positions}
📝 <b>挂单数：</b>{open_orders}
━━━━━━━━━━━━━━━━━━━━━━━━━

🕐 <b>报告时间：</b>{_now_shanghai().strftime('%H:%M UTC+8')}
"""
        await self.send_message(message.strip())
    
    async def send_error_notification(self, error_message: str):
        """Send error notification"""
        message = f"""
⚠️ <b>检测到错误</b>

<code>{error_message}</code>

<b>时间：</b>{_now_shanghai().strftime('%H:%M:%S UTC+8')}
"""
        await self.send_message(message.strip())
    
    async def send_startup_notification(
        self,
        target_wallet: str,
        sizing_mode: str,
        ratio: str,
        leverage_adjustment: float,
        existing_position_policy: str = "",
        funding_warning: str = "",
    ):
        """Send bot startup notification"""
        policy_line = (
            f"• 追入旧仓位：{existing_position_policy}\n"
            if existing_position_policy
            else ""
        )
        status_line = (
            "💸 <b>跟单已停止（钱包无资金）</b>\n\n"
            f"{funding_warning}\n\n"
            "/orders、/copyloss、/close 已禁用；入金后发 /start 恢复。"
            if funding_warning
            else "• 状态：<b>运行中</b> 🟢\n\n正在监听目标钱包的新挂单、撤单和成交。"
        )
        message = f"""
🚀 <b>跟单机器人已启动</b>

<b>目标钱包：</b>
<code>{target_wallet}</code>

<b>当前配置：</b>
• 仓位模式：{sizing_mode.title()}
• 资金比例：{ratio}
• 杠杆：目标杠杆的 {leverage_adjustment} 倍
{policy_line}{status_line}
"""
        await self.send_message(message.strip())
    
    async def send_shutdown_notification(self):
        """Send bot shutdown notification"""
        message = """
🛑 <b>跟单机器人已停止</b>

机器人已安全退出。
状态：<b>已停止</b> 🔴
"""
        await self.send_message(message.strip())
    
    def enable(self):
        """Enable notifications"""
        self.enabled = True
        logger.info("Notifications enabled")
    
    def disable(self):
        """Disable notifications"""
        self.enabled = False
        logger.info("Notifications disabled")
