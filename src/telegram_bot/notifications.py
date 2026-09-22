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
    ) -> bool:
        """Notify about a mirrored target order before it is filled."""
        leverage_text = f"{leverage:g}x"
        target_leverage_text = (
            f"{target_leverage:g}x" if target_leverage is not None else leverage_text
        )
        message = f"""
🧪 <b>检测到新镜像订单</b> [{status}]

<b>币种：</b>{symbol}
<b>方向：</b>{side.upper()}
<b>跟随数量：</b>{size:.4f}
<b>挂单价格：</b>${entry_price:,.2f}
<b>目标杠杆：</b>{target_leverage_text}
<b>跟随杠杆：</b>{leverage_text}
<b>名义价值：</b>${size * entry_price:,.2f}

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
        target_wallet: str
    ):
        """Send hourly trading report"""
        
        pnl_emoji = "📈" if account_pnl_usd > 0 else "📉"
        
        message = f"""
📊 <b>每小时跟单报告</b>

<b>目标：</b><code>{target_wallet[:10]}...{target_wallet[-6:]}</code>

━━━━━━━━━━━━━━━━━━━━━━━━━
📈 <b>已复制成交：</b>{trades_copied}
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
        leverage_adjustment: float
    ):
        """Send bot startup notification"""
        message = f"""
🚀 <b>跟单机器人已启动</b>

<b>目标钱包：</b>
<code>{target_wallet}</code>

<b>当前配置：</b>
• 仓位模式：{sizing_mode.title()}
• 资金比例：{ratio}
• 杠杆：目标杠杆的 {leverage_adjustment} 倍
• 状态：<b>运行中</b> 🟢

正在监听目标钱包的新挂单、撤单和成交。
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
