import asyncio
import re
from typing import Optional, Callable
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from loguru import logger


class TelegramBot:
    """
    Telegram bot for controlling and monitoring the copy trader
    """
    
    def __init__(
        self,
        bot_token: str,
        allowed_chat_id: str
    ):
        """
        Initialize Telegram bot
        
        Args:
            bot_token: Telegram bot token from BotFather
            allowed_chat_id: Only this chat ID can control the bot
        """
        self.bot_token = bot_token
        self.allowed_chat_id = str(allowed_chat_id)
        self.app: Optional[Application] = None
        
        # Callbacks that main app can set
        self.on_stop_requested: Optional[Callable] = None
        self.on_pause_requested: Optional[Callable] = None
        self.on_resume_requested: Optional[Callable] = None
        self.get_status_callback: Optional[Callable] = None
        self.get_positions_callback: Optional[Callable] = None
        self.get_orders_callback: Optional[Callable] = None
        self.get_pnl_callback: Optional[Callable] = None
        self.get_leaderboard_callback: Optional[Callable] = None
        self.get_wallet_callback: Optional[Callable] = None
        
        logger.info(f"Telegram bot initialized for chat {allowed_chat_id}")
    
    def _check_authorized(self, update: Update) -> bool:
        """Check if user is authorized"""
        user_chat_id = str(update.effective_chat.id)
        if user_chat_id != self.allowed_chat_id:
            logger.warning(f"Unauthorized access attempt from chat {user_chat_id}")
            return False
        return True
    
    async def _start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        message = """
🤖 <b>Hyperliquid 跟单机器人</b>

<b>可用命令：</b>

/status - 查看运行状态
/positions - 查看当前持仓
/orders - 查看当前挂单
/pnl - 查看收益摘要
/leaderboard - 查看公开收益排行榜
/wallet 地址 [数量] - 查询公开账户收益和最近成交
/pause - 暂停复制新成交，保留仓位
/resume - 恢复复制
/stop - 停止机器人，可选择是否平仓

<b>状态：</b>🟢 运行中
        """
        await update.message.reply_text(message.strip(), parse_mode="HTML")
    
    async def _status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        if self.get_status_callback:
            try:
                status = await self.get_status_callback()
                await update.message.reply_text(status, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error getting status: {e}")
                await update.message.reply_text(f"❌ 获取状态失败：{e}")
        else:
            await update.message.reply_text("状态查询尚未配置")
    
    async def _positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /positions command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        if self.get_positions_callback:
            try:
                positions = await self.get_positions_callback()
                await update.message.reply_text(positions, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error getting positions: {e}")
                await update.message.reply_text(f"❌ 获取仓位失败：{e}")
        else:
            await update.message.reply_text("📍 仓位查询尚未配置")
    
    async def _orders_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /orders command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        if self.get_orders_callback:
            try:
                orders = await self.get_orders_callback()
                
                if not orders:
                    await update.message.reply_text("📋 当前没有挂单")
                    return
                
                message = "<b>当前挂单</b>\n\n"
                for i, order in enumerate(orders, 1):
                    side = order.get('side', 'BUY').upper()
                    order_type = order.get('order_type', 'LIMIT').upper()
                    
                    message += f"<b>{i}. {order['symbol']} {side}</b>\n"
                    message += f"   类型：{order_type}\n"
                    message += f"   数量：{abs(order['size']):.4f}\n"
                    price = order.get('price')
                    message += f"   价格：${price:,.2f}\n" if price is not None else "   价格：市价\n"
                    
                    if 'trigger_price' in order and order['trigger_price']:
                        message += f"   触发价：${order['trigger_price']:,.2f}\n"
                    
                    message += "\n"
                
                await update.message.reply_text(message.strip(), parse_mode="HTML")
            except Exception as e:
                logger.error(f"Error getting orders: {e}")
                await update.message.reply_text(f"❌ 获取挂单失败：{e}")
        else:
            await update.message.reply_text("📋 挂单查询尚未配置")
    
    async def _pause_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pause command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        if self.on_pause_requested:
            try:
                await self.on_pause_requested()
                await update.message.reply_text(
                    "⏸️ <b>机器人已暂停</b>\n\n不会再复制新成交。\n已有仓位保持不变。",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error pausing: {e}")
                await update.message.reply_text(f"❌ 暂停失败：{e}")
        else:
            await update.message.reply_text("暂停功能尚未配置")
    
    async def _resume_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /resume command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        if self.on_resume_requested:
            try:
                await self.on_resume_requested()
                await update.message.reply_text(
                    "▶️ <b>机器人已恢复</b>\n\n正在继续复制新成交。",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Error resuming: {e}")
                await update.message.reply_text(f"❌ 恢复失败：{e}")
        else:
            await update.message.reply_text("恢复功能尚未配置")
    
    async def _stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command - show confirmation"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        
        keyboard = [
            [
                InlineKeyboardButton("平掉全部仓位", callback_data="stop_close"),
                InlineKeyboardButton("保留现有仓位", callback_data="stop_keep")
            ],
            [InlineKeyboardButton("❌ 取消", callback_data="stop_cancel")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ <b>停止跟单</b>\n\n"
            "将会：\n"
            "✅ 停止复制新成交\n"
            "✅ 取消全部挂单\n\n"
            "是否同时平掉全部仓位？",
            reply_markup=reply_markup,
            parse_mode="HTML"
        )

    @staticmethod
    def _leaderboard_keyboard(
        window: Optional[str] = None,
        sort_by: Optional[str] = None,
        page: int = 0,
        total_pages: int = 0,
    ) -> InlineKeyboardMarkup:
        keyboard = [
            [
                InlineKeyboardButton("24H 收益", callback_data="leaderboard:day:pnl:0"),
                InlineKeyboardButton("24H 收益率", callback_data="leaderboard:day:roi:0"),
            ],
            [
                InlineKeyboardButton("7D 收益", callback_data="leaderboard:week:pnl:0"),
                InlineKeyboardButton("7D 收益率", callback_data="leaderboard:week:roi:0"),
            ],
            [
                InlineKeyboardButton("30D 收益", callback_data="leaderboard:month:pnl:0"),
                InlineKeyboardButton("30D 收益率", callback_data="leaderboard:month:roi:0"),
            ],
        ]
        if window and sort_by and total_pages > 1:
            navigation = []
            if page > 0:
                navigation.append(InlineKeyboardButton("上一页", callback_data=f"leaderboard:{window}:{sort_by}:{page - 1}"))
            navigation.append(InlineKeyboardButton(f"第 {page + 1}/{total_pages} 页", callback_data="leaderboard:noop"))
            if page + 1 < total_pages:
                navigation.append(InlineKeyboardButton("下一页", callback_data=f"leaderboard:{window}:{sort_by}:{page + 1}"))
            keyboard.append(navigation)
        return InlineKeyboardMarkup(keyboard)

    async def _leaderboard_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show period and sort selectors for the public leaderboard."""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        await update.message.reply_text(
            "🏆 <b>Hyperliquid 收益排行榜</b>\n\n请选择周期和排序方式：",
            reply_markup=self._leaderboard_keyboard(),
            parse_mode="HTML",
        )
    
    async def _button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        if not self._check_authorized(update):
            await query.edit_message_text("⛔ 未授权的聊天")
            return

        if query.data == "leaderboard:noop":
            return

        if query.data and query.data.startswith("leaderboard:"):
            parts = query.data.split(":")
            if len(parts) != 4 or parts[1] not in {"day", "week", "month"} or parts[2] not in {"pnl", "roi"}:
                await query.edit_message_text("❌ 无效的排行榜选项")
                return
            try:
                page = int(parts[3])
            except ValueError:
                await query.edit_message_text("❌ 无效的排行榜页码")
                return
            if not 0 <= page < 20:
                await query.edit_message_text("❌ 无效的排行榜页码")
                return
            if not self.get_leaderboard_callback:
                await query.edit_message_text("排行榜查询尚未配置")
                return
            try:
                await query.edit_message_text(
                    "🏆 <b>Hyperliquid 收益排行榜</b>\n\n正在读取公开数据...",
                    parse_mode="HTML",
                )
                result, total_pages = await self.get_leaderboard_callback(parts[1], parts[2], page)
                await query.edit_message_text(
                    result,
                    reply_markup=self._leaderboard_keyboard(parts[1], parts[2], page, total_pages),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Error getting leaderboard: {e}")
                await query.edit_message_text(f"❌ 获取排行榜失败：{e}")
            return
        
        if query.data == "stop_close":
            await query.edit_message_text(
                "🛑 <b>正在停止机器人...</b>\n\n"
                "• 正在取消全部挂单\n"
                "• 正在平掉全部仓位\n"
                "• 正在退出\n\n"
                "请稍候...",
                parse_mode="HTML"
            )
            if self.on_stop_requested:
                try:
                    await self.on_stop_requested(close_positions=True)
                    await query.edit_message_text(
                        "✅ <b>机器人已停止</b>\n\n"
                        "全部挂单已取消。\n"
                        "全部仓位已平。\n"
                        "状态：🔴 已停止",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ 停止失败：{e}")
        
        elif query.data == "stop_keep":
            await query.edit_message_text(
                "🛑 <b>正在停止机器人...</b>\n\n"
                "• 正在取消全部挂单\n"
                "• 保留现有仓位\n"
                "• 正在退出\n\n"
                "请稍候...",
                parse_mode="HTML"
            )
            if self.on_stop_requested:
                try:
                    await self.on_stop_requested(close_positions=False)
                    await query.edit_message_text(
                        "✅ <b>机器人已停止</b>\n\n"
                        "全部挂单已取消。\n"
                        "仓位已保留。\n"
                        "状态：🔴 已停止",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ 停止失败：{e}")
        
        elif query.data == "stop_cancel":
            await query.edit_message_text(
                "✅ 已取消停止操作，机器人仍在运行。",
                parse_mode="HTML"
            )
    
    async def _pnl_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /pnl command"""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return

        if not self.get_pnl_callback:
            await update.message.reply_text("收益查询尚未配置")
            return

        try:
            pnl = await self.get_pnl_callback()
            await update.message.reply_text(pnl, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error getting PnL: {e}")
            await update.message.reply_text(f"❌ 获取收益失败：{e}")

    async def _wallet_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show public account performance and newest fills for an address."""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        if not context.args:
            await update.message.reply_text("用法：<code>/wallet 0x钱包地址 [1-20]</code>", parse_mode="HTML")
            return
        address = context.args[0].strip().lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", address):
            await update.message.reply_text("❌ 地址格式无效，请输入 0x 开头的 40 位十六进制钱包地址。")
            return
        limit = 10
        if len(context.args) > 1:
            try:
                limit = int(context.args[1])
            except ValueError:
                await update.message.reply_text("❌ 成交数量必须是 1 到 20 的整数。")
                return
        if not 1 <= limit <= 20:
            await update.message.reply_text("❌ 成交数量必须是 1 到 20。")
            return
        if not self.get_wallet_callback:
            await update.message.reply_text("账户查询尚未配置")
            return
        try:
            await update.message.reply_text("🔎 正在读取公开账户数据...", parse_mode="HTML")
            result = await self.get_wallet_callback(address, limit)
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Error getting public wallet data: {e}")
            await update.message.reply_text(f"❌ 获取账户数据失败：{e}")
    
    async def start(self):
        """Start the Telegram bot"""
        logger.info("Starting Telegram bot...")
        
        # Create application
        self.app = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.app.add_handler(CommandHandler("start", self._start_command))
        self.app.add_handler(CommandHandler("status", self._status_command))
        self.app.add_handler(CommandHandler("positions", self._positions_command))
        self.app.add_handler(CommandHandler("orders", self._orders_command))
        self.app.add_handler(CommandHandler("pause", self._pause_command))
        self.app.add_handler(CommandHandler("resume", self._resume_command))
        self.app.add_handler(CommandHandler("stop", self._stop_command))
        self.app.add_handler(CommandHandler("pnl", self._pnl_command))
        self.app.add_handler(CommandHandler("leaderboard", self._leaderboard_command))
        self.app.add_handler(CommandHandler("wallet", self._wallet_command))
        
        # Add callback query handler for buttons
        self.app.add_handler(CallbackQueryHandler(self._button_callback))
        
        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        
        logger.info("✅ Telegram bot started and polling")
    
    async def stop(self):
        """Stop the Telegram bot"""
        if self.app:
            logger.info("Stopping Telegram bot...")
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            logger.info("Telegram bot stopped")
