import asyncio
import re
from typing import Optional, Callable
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
from loguru import logger

from version import build_stamp


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
        self.on_start_requested: Optional[Callable] = None
        self.on_stop_requested: Optional[Callable] = None
        self.on_close_positions_requested: Optional[Callable] = None
        self.on_follow_requested: Optional[Callable] = None
        self.on_copy_existing_requested: Optional[Callable] = None
        self.get_status_callback: Optional[Callable] = None
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
        """Handle /start: resume/start copying and show the command reference.

        Merged from the old /start (help only) and /resume: this is now the
        single "make it run again" command, for both a paused and a stopped bot.
        """
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return

        state_line = "🟢 运行中"
        if self.on_start_requested:
            try:
                state_line = await self.on_start_requested()
            except Exception as e:
                logger.error(f"Error starting bot: {e}")
                state_line = f"❌ 启动失败：{e}"

        message = f"""
🤖 <b>Hyperliquid 跟单机器人</b>

<b>可用命令：</b>

/start - 启动或恢复跟单（暂停/停止后都用它）
/status - 查看运行状态
/orders - 查看当前挂单
/pnl - 查看收益摘要
/leaderboard - 查看公开收益排行榜
/wallet 地址 [数量] - 查询公开账户收益和最近成交
/follow 地址 - 更换跟单目标钱包
/copyloss [loss|all|profit] [序号...] [confirm] [force] - 手动复制目标现有仓位：先列清单，可只买指定序号，执行后回执；已持有或刚复制过的会自动拦下，确需叠加再加 force
/close [序号|币种|all] [confirm] - 平掉自己的仓位：先列清单，可选币种或序号，执行后回执
/stop - 停止跟单：撤掉挂单，保留仓位（发 /start 恢复）

<b>状态：</b>{state_line}
<b>版本：</b>{build_stamp()}
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

    async def _follow_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Validate and switch the target wallet used for copying."""
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        if len(context.args) != 1:
            await update.message.reply_text(
                "用法：<code>/follow 0x目标钱包地址</code>", parse_mode="HTML"
            )
            return
        if not self.on_follow_requested:
            await update.message.reply_text("更换跟单地址功能尚未配置")
            return
        try:
            result = await self.on_follow_requested(context.args[0])
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception as exc:
            logger.error(f"Error switching follow wallet: {exc}")
            await update.message.reply_text(f"❌ 更换跟单地址失败：{exc}")
    
    async def _copyloss_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /copyloss - copy the target's open positions, on demand.

        Two steps on purpose: without ``confirm`` it only lists what would be
        bought, because in live mode this places real market orders. Numbers in
        the arguments pick a subset of that list, e.g. ``/copyloss 1 3 confirm``.

        ``force`` bypasses the "already copied this symbol" guard once; the
        duplicate check itself lives in main so every caller gets it.
        """
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        if not self.on_copy_existing_requested:
            await update.message.reply_text("一键复制功能尚未配置")
            return

        args = [arg.strip().lower() for arg in (context.args or [])]
        # None means "use the configured default", resolved in main so the
        # command and the /status policy line cannot disagree.
        mode = None
        confirm = False
        force = False
        selection: list[int] = []
        for arg in args:
            if arg in ("confirm", "go", "yes", "执行"):
                confirm = True
                continue
            if arg in ("force", "强制", "重复"):
                force = True
                continue
            numbers = [part for part in re.split(r"[,，、\s]+", arg) if part]
            if numbers and all(part.isdigit() for part in numbers):
                selection.extend(int(part) for part in numbers)
                continue
            mode = arg

        try:
            await update.message.reply_text("⏳ 正在读取目标持仓...", parse_mode="HTML")
            result = await self.on_copy_existing_requested(
                mode, confirm, selection or None, force
            )
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception as exc:
            logger.error(f"Error in one-shot copy: {exc}")
            await update.message.reply_text(f"❌ 一键复制失败：{exc}")

    async def _positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Keep /positions as a backward-compatible alias for /status."""
        await self._status_command(update, context)
    
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
    
    async def _close_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /close - close the follower's own positions, on demand.

        Two steps on purpose: without ``confirm`` it only lists what would be
        closed, because in live mode every item is a real reduce-only market
        order. Items can be picked by 1-based number or by (partial) symbol,
        e.g. ``/close 1 3 confirm`` or ``/close BTC confirm``; ``all`` closes
        everything. Works while the bot is paused or stopped — that is the
        point, since /stop no longer closes anything.
        """
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return
        if not self.on_close_positions_requested:
            await update.message.reply_text("平仓功能尚未配置")
            return

        args = [arg.strip() for arg in (context.args or [])]
        confirm = False
        selection: list[str] = []
        for arg in args:
            if arg.lower() in ("confirm", "go", "yes", "执行"):
                confirm = True
                continue
            for part in re.split(r"[,，、\s]+", arg):
                if part:
                    selection.append(part)

        if not confirm and not selection:
            # Bare /close or /close confirm with nothing else: still useful as
            # the list view, so fall through with an empty selection.
            selection = None
        try:
            await update.message.reply_text("⏳ 正在读取跟随账户持仓...", parse_mode="HTML")
            result = await self.on_close_positions_requested(selection, confirm)
            await update.message.reply_text(result, parse_mode="HTML")
        except Exception as exc:
            logger.error(f"Error closing positions: {exc}")
            await update.message.reply_text(f"❌ 平仓失败：{exc}")

    async def _stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop: stop copying immediately, keep positions.

        Merged from the old /stop (which asked whether to close everything via
        buttons) and /pause: stopping now never touches positions, and closing
        has its own command in /close, so there is no confirmation step.
        """
        if not self._check_authorized(update):
            await update.message.reply_text("⛔ 未授权的聊天")
            return

        if not self.on_stop_requested:
            await update.message.reply_text("停止功能尚未配置")
            return

        try:
            await self.on_stop_requested()
            await update.message.reply_text(
                "🛑 <b>机器人已停止</b>\n\n"
                "• 已停止复制新成交\n"
                "• 全部挂单已取消\n"
                "• 仓位全部保留（要平仓请用 /close）\n\n"
                "发 <code>/start</code> 恢复跟单。",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")
            await update.message.reply_text(f"❌ 停止失败：{e}")

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
        
        if query.data == "stop_close" or query.data == "stop_keep" or query.data == "stop_cancel":
            # Legacy buttons from the old /stop dialog. /stop no longer shows
            # buttons, but an old message may still be sitting in the chat.
            await query.edit_message_text(
                "ℹ️ 停止命令已更新：/stop 直接停止并保留仓位，平仓请用 /close。"
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
            await update.message.reply_text("用法：<code>/wallet 0x钱包地址 [1-30]</code>", parse_mode="HTML")
            return
        address = context.args[0].strip().lower()
        if not re.fullmatch(r"0x[a-f0-9]{40}", address):
            await update.message.reply_text("❌ 地址格式无效，请输入 0x 开头的 40 位十六进制钱包地址。")
            return
        limit = 30
        if len(context.args) > 1:
            try:
                limit = int(context.args[1])
            except ValueError:
                await update.message.reply_text("❌ 成交数量必须是 1 到 30 的整数。")
                return
        if not 1 <= limit <= 30:
            await update.message.reply_text("❌ 成交数量必须是 1 到 30。")
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
    
    @staticmethod
    def _command_menu() -> list[BotCommand]:
        """The command menu Telegram shows in the "/" picker.

        Every user-facing CommandHandler registered in :meth:`start` must
        appear here, otherwise the command works but is undiscoverable from
        the client. Pure aliases such as ``/positions`` (a synonym for
        ``/status``) are left out on purpose.
        """
        return [
            BotCommand("start", "启动或恢复跟单"),
            BotCommand("status", "查看运行状态和自己的持仓"),
            BotCommand("orders", "查看当前挂单"),
            BotCommand("pnl", "查看收益摘要"),
            BotCommand("wallet", "查询公开账户"),
            BotCommand("follow", "更换跟单目标钱包"),
            BotCommand("copyloss", "手动复制目标现有仓位（可选序号，带回执）"),
            BotCommand("close", "平掉自己的仓位（可选币种/序号，带回执）"),
            BotCommand("leaderboard", "查看收益排行榜"),
            BotCommand("stop", "停止跟单，保留仓位"),
        ]

    async def _publish_command_menu(self) -> None:
        """Publish the command menu and read it back so the log proves it stuck.

        Telegram stores the menu per scope. Publishing only to the authorized
        chat leaves the default scope (what the client shows in any other chat
        with the bot) on whatever BotFather had, which looks like "the command
        is missing". Both scopes are written here; handlers still reject
        unauthorized chats.
        """
        commands = self._command_menu()
        targets = [
            ("默认范围", None),
            (f"聊天 {self.allowed_chat_id}", self.allowed_chat_id),
        ]
        for label, chat_id in targets:
            scope = BotCommandScopeChat(chat_id=chat_id) if chat_id else None
            try:
                if scope is None:
                    await self.app.bot.set_my_commands(commands)
                else:
                    await self.app.bot.set_my_commands(commands, scope=scope)
            except Exception as exc:
                # A silent menu failure looks exactly like "the command was
                # never added", so this must not stay at WARNING.
                logger.error(f"Failed to publish Telegram command menu ({label}): {exc}")
                continue

            try:
                stored = (
                    await self.app.bot.get_my_commands(scope=scope)
                    if scope is not None
                    else await self.app.bot.get_my_commands()
                )
            except Exception as exc:
                logger.warning(
                    f"Command menu published ({label}) but read-back failed: {exc}"
                )
                continue

            names = [item.command for item in stored]
            logger.success(f"Telegram command menu ({label}): {', '.join(names)}")
            missing = [item.command for item in commands if item.command not in names]
            if missing:
                logger.error(
                    f"Telegram command menu ({label}) is missing: {', '.join(missing)}"
                )

    async def start(self):
        """Start the Telegram bot"""
        logger.info("Starting Telegram bot...")
        
        # Create application
        self.app = Application.builder().token(self.bot_token).build()
        
        # Add command handlers
        self.app.add_handler(CommandHandler("start", self._start_command))
        self.app.add_handler(CommandHandler("status", self._status_command))
        self.app.add_handler(CommandHandler("follow", self._follow_command))
        self.app.add_handler(CommandHandler("copyloss", self._copyloss_command))
        self.app.add_handler(CommandHandler("positions", self._positions_command))
        self.app.add_handler(CommandHandler("orders", self._orders_command))
        self.app.add_handler(CommandHandler("close", self._close_command))
        self.app.add_handler(CommandHandler("stop", self._stop_command))
        self.app.add_handler(CommandHandler("pnl", self._pnl_command))
        self.app.add_handler(CommandHandler("leaderboard", self._leaderboard_command))
        self.app.add_handler(CommandHandler("wallet", self._wallet_command))
        
        # Add callback query handler for buttons
        self.app.add_handler(CallbackQueryHandler(self._button_callback))
        
        # Start polling
        await self.app.initialize()
        # After initialize() so a token/network problem surfaces here instead
        # of being swallowed while the menu stays empty.
        await self._publish_command_menu()
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
