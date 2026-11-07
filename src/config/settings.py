import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

_UNLIMITED_TOKENS = {'x', 'none', 'unlimited', 'off', 'false'}

# Which existing target positions may be copied at startup.
#   all    -> copy every open position (historical behaviour)
#   loss   -> only positions the target is currently underwater on
#   profit -> only positions the target is currently in profit on
_PNL_FILTER_ALIASES = {
    'all': 'all', 'any': 'all', 'both': 'all', 'none': 'all', 'off': 'all',
    'loss': 'loss', 'losing': 'loss', 'losers': 'loss',
    'underwater': 'loss', 'negative': 'loss', 'red': 'loss',
    'profit': 'profit', 'profitable': 'profit', 'winning': 'profit',
    'winners': 'profit', 'positive': 'profit', 'green': 'profit',
}


def normalize_pnl_filter(value: Optional[str], default: str = 'all') -> str:
    """Map a user-facing existing-position filter to 'all' | 'loss' | 'profit'."""
    if value is None or not str(value).strip():
        return default
    token = str(value).strip().lower()
    if token in _PNL_FILTER_ALIASES:
        return _PNL_FILTER_ALIASES[token]
    raise ValueError(
        "COPY_OPEN_PNL_FILTER must be one of all/loss/profit, "
        f"got {value!r}"
    )


def _optional_limit(name: str, default: Optional[float]) -> Optional[float]:
    """Read an optional dollar cap from the environment.

    ``x`` / ``none`` / ``unlimited`` / ``off`` and any non-positive number all
    mean "no cap" and return ``None``. An omitted variable keeps the default so
    existing deployments are unaffected.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    token = raw.strip().lower()
    if token in _UNLIMITED_TOKENS:
        return None
    value = float(token)
    return value if value > 0 else None


def _non_negative_float(name: str, default: float) -> float:
    """Read a non-negative float, raising on garbage (fail at startup)."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {raw!r}")
    return value


def _non_negative_int(name: str, default: int) -> int:
    """Read a non-negative integer, raising on garbage.

    Used by safety switches: a typo must fail at startup instead of silently
    disabling the guard. ``0`` is a meaningful value (guard off).
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(float(raw.strip()))
    except ValueError:
        raise ValueError(f"{name} must be a number of minutes, got {raw!r}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0, got {raw!r}")
    return value


class HyperliquidConfig(BaseModel):
    api_url: str = Field(default="https://api.hyperliquid.xyz")
    ws_url: str = Field(default="wss://api.hyperliquid.xyz/ws")
    leaderboard_url: str = Field(default="https://stats-data.hyperliquid.xyz/Mainnet/leaderboard")
    wallet_address: Optional[str] = None
    private_key: Optional[str] = None
    account_mode: str = "standard"  # follower account: "standard" or "unified"

class TelegramConfig(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    report_interval_hours: int = 1

class SizingConfig(BaseModel):
    mode: str = "proportional"  # "fixed" or "proportional"
    fixed_size: float = 100.0
    portfolio_ratio: float = 0.01  # 1:100 ratio
    # Per-position notional cap. None = uncapped: the only bound is the
    # exchange's own available margin, so the copied size keeps the target's
    # risk profile instead of being flattened to a fixed dollar amount.
    max_position_size: Optional[float] = None
    # Aggregate notional cap across all positions. None = uncapped; the real
    # risk bound is MAX_MARGIN_USAGE_RATIO against the exchange's own margin.
    max_total_exposure: Optional[float] = None

class LeverageConfig(BaseModel):
    adjustment_ratio: float = 0.5
    max_leverage: float = 10.0
    min_leverage: float = 1.0

class CopyRulesConfig(BaseModel):
    copy_existing_positions: bool = True
    copy_existing_orders: bool = True
    # Automatic catch-up of the target's already-open positions at startup.
    # Off by default: it fires real market orders before anyone can look at
    # them, and the manual equivalent (/copyloss) lets the user pick which
    # positions to take and returns a fill receipt.
    copy_open_positions: bool = False
    # Scope /copyloss lists by default: "all" | "loss" | "profit".
    # "loss" shows only the positions the target is currently losing on, so
    # the follower enters at a better price than the target's own entry.
    copy_open_pnl_filter: str = "loss"
    # Refuse to copy the same symbol/side again within this many minutes.
    # 0 disables the guard. /copyloss ... force bypasses it once.
    copy_duplicate_guard_minutes: int = 30
    # Mirror target resting orders immediately and cancel the follower order
    # when the target order is cancelled/expired/rejected.
    mirror_pending_orders: bool = True
    cancel_mirrored_orders: bool = True
    mirror_order_price: bool = True
    auto_adjust_size: bool = True
    use_limit_orders: bool = False  # Convert market orders to limit orders at fill price
    max_open_trades: Optional[int] = None  # None = unlimited
    max_open_orders: Optional[int] = None  # None = unlimited
    max_account_equity: Optional[float] = None  # None = unlimited
    min_entry_quality_pct: float = 5.0
    max_slippage_pct: float = 1.0
    # Reserve headroom for asset-specific initial-margin requirements and fees.
    max_margin_usage_ratio: float = 0.40
    fill_polling_enabled: bool = False
    fill_poll_interval_seconds: int = 15
    # Coalesce bursts of same-intent target orders into one follower order.
    pending_order_merge_window_seconds: float = 5.0
    min_position_size_usd: float = 10.0
    blocked_assets: list[str] = []  # Assets to NOT copy (e.g., ["BTC", "ETH"])

class RiskManagementConfig(BaseModel):
    max_concurrent_positions: int = 10
    max_daily_loss_usd: float = 500.0
    enable_custom_stops: bool = False
    stop_loss_pct: float = 5.0


class TargetEquityFlowConfig(BaseModel):
    """Watch the followed wallet for deposits/withdrawals.

    The follower's copy ratio is derived from the target's account value, so
    an unnoticed withdrawal or deposit silently resizes every future copy.
    Detection compares account equity between two polls, removes the change
    caused by unrealized PnL, and only alerts if positions stayed put.
    """

    enabled: bool = True
    poll_seconds: int = 60
    # Noise thresholds for the *inferred* residual only: an absolute floor
    # avoids noise on small accounts, a percentage floor keeps large accounts
    # from missing a proportionally small but meaningful transfer. A ledger
    # confirmed deposit/withdrawal bypasses both (see LEDGER_ALERT_MIN_USD),
    # because a real cash movement is never noise.
    min_usd: float = 100.0
    min_pct: float = 1.0
    # Max tolerated change in position sizes (percent of gross signed size).
    max_position_change_pct: float = 5.0
    cooldown_seconds: int = 300


class Settings(BaseModel):
    # Target address to copy (wallet or vault - the bot treats them the same)
    target_wallet: str = "0x0ba5de43fa2419a25c2e680f84aff3a8f57fce22"
    
    # Trading mode
    simulated_trading: bool = True
    simulated_account_balance: float = 1000.0
    
    # Configuration sections
    hyperliquid: HyperliquidConfig = Field(default_factory=HyperliquidConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    sizing: SizingConfig = Field(default_factory=SizingConfig)
    leverage: LeverageConfig = Field(default_factory=LeverageConfig)
    copy_rules: CopyRulesConfig = Field(default_factory=CopyRulesConfig)
    risk_management: RiskManagementConfig = Field(default_factory=RiskManagementConfig)
    target_equity_flow: TargetEquityFlowConfig = Field(default_factory=TargetEquityFlowConfig)
    
    # Paths
    log_level: str = "INFO"
    log_file: str = "./logs/trading.log"
    database_url: str = "sqlite:///./data/trading.db"
    
    class Config:
        env_file = '.env'
        env_file_encoding = 'utf-8'

    @classmethod
    def load(cls) -> 'Settings':
        """Load settings from environment variables"""
        settings = cls()
        
        # Load from environment
        settings.hyperliquid.api_url = os.getenv('HYPERLIQUID_API_URL', settings.hyperliquid.api_url)
        settings.hyperliquid.leaderboard_url = os.getenv(
            'HYPERLIQUID_LEADERBOARD_URL', settings.hyperliquid.leaderboard_url
        )
        settings.hyperliquid.wallet_address = os.getenv('HYPERLIQUID_WALLET_ADDRESS')
        settings.hyperliquid.private_key = os.getenv('HYPERLIQUID_PRIVATE_KEY')
        account_mode = os.getenv('HYPERLIQUID_ACCOUNT_MODE', 'standard').strip().lower()
        if account_mode not in {'standard', 'unified'}:
            raise ValueError(
                "HYPERLIQUID_ACCOUNT_MODE must be either 'standard' or 'unified'"
            )
        settings.hyperliquid.account_mode = account_mode
        
        settings.target_wallet = os.getenv('TARGET_WALLET_ADDRESS', settings.target_wallet)
        
        # Trading mode
        sim_trading = os.getenv('SIMULATED_TRADING', 'true').lower()
        settings.simulated_trading = sim_trading in ('true', '1', 'yes')
        
        sim_balance = os.getenv('SIMULATED_ACCOUNT_BALANCE', '1000.0')
        settings.simulated_account_balance = float(sim_balance)
        
        # Copy trading settings
        copy_open_pos = os.getenv('COPY_OPEN_POSITIONS', 'false').lower()
        settings.copy_rules.copy_open_positions = copy_open_pos in ('true', '1', 'yes')
        settings.copy_rules.copy_open_pnl_filter = normalize_pnl_filter(
            os.getenv('COPY_OPEN_PNL_FILTER'),
            # Honour the model default (loss) when the variable is absent;
            # normalize_pnl_filter's own default is "all" and used to override
            # it, so an empty .env silently listed every position.
            default=settings.copy_rules.copy_open_pnl_filter,
        )
        settings.copy_rules.copy_duplicate_guard_minutes = _non_negative_int(
            'COPY_DUPLICATE_GUARD_MINUTES', default=30
        )
        
        copy_orders = os.getenv('COPY_EXISTING_ORDERS', 'true').lower()
        settings.copy_rules.copy_existing_orders = copy_orders in ('true', '1', 'yes')

        mirror_pending = os.getenv('COPY_PENDING_ORDERS', 'true').lower()
        settings.copy_rules.mirror_pending_orders = mirror_pending in ('true', '1', 'yes')
        cancel_mirrored = os.getenv('CANCEL_MIRRORED_ORDERS', 'true').lower()
        settings.copy_rules.cancel_mirrored_orders = cancel_mirrored in ('true', '1', 'yes')
        mirror_price = os.getenv('MIRROR_ORDER_PRICE', 'true').lower()
        settings.copy_rules.mirror_order_price = mirror_price in ('true', '1', 'yes')
        
        auto_adjust = os.getenv('AUTO_ADJUST_SIZE', 'true').lower()
        settings.copy_rules.auto_adjust_size = auto_adjust in ('true', '1', 'yes')
        
        use_limit = os.getenv('USE_LIMIT_ORDERS', 'false').lower()
        settings.copy_rules.use_limit_orders = use_limit in ('true', '1', 'yes')

        max_slippage = os.getenv('MAX_SLIPPAGE_PCT', '1.0')
        settings.copy_rules.max_slippage_pct = float(max_slippage)

        max_margin_usage = float(os.getenv('MAX_MARGIN_USAGE_RATIO', '0.40'))
        settings.copy_rules.max_margin_usage_ratio = min(0.95, max(0.05, max_margin_usage))

        fill_polling = os.getenv('FILL_POLLING_ENABLED', 'false').lower()
        settings.copy_rules.fill_polling_enabled = fill_polling in ('true', '1', 'yes')
        settings.copy_rules.fill_poll_interval_seconds = max(
            15, int(os.getenv('FILL_POLL_INTERVAL_SECONDS', '15'))
        )
        settings.copy_rules.pending_order_merge_window_seconds = min(
            60.0,
            max(0.0, float(os.getenv('PENDING_ORDER_MERGE_WINDOW_SECONDS', '5'))),
        )
        
        # Leverage adjustment
        leverage_adj = os.getenv('LEVERAGE_ADJUSTMENT', '0.5')
        settings.leverage.adjustment_ratio = float(leverage_adj)

        settings.sizing.max_position_size = _optional_limit(
            'MAX_POSITION_SIZE', settings.sizing.max_position_size
        )
        settings.sizing.max_total_exposure = _optional_limit(
            'MAX_TOTAL_EXPOSURE', settings.sizing.max_total_exposure
        )
        
        max_trades = os.getenv('MAX_OPEN_TRADES', 'x')
        settings.copy_rules.max_open_trades = None if max_trades.lower() == 'x' else int(max_trades)
        
        max_orders = os.getenv('MAX_OPEN_ORDERS', 'x')
        settings.copy_rules.max_open_orders = None if max_orders.lower() == 'x' else int(max_orders)
        
        max_equity = os.getenv('MAX_ACCOUNT_EQUITY', 'x')
        settings.copy_rules.max_account_equity = None if max_equity.lower() == 'x' else float(max_equity)
        
        # Target wallet fund-flow monitoring (deposit / withdrawal alerts).
        flow = settings.target_equity_flow
        flow.enabled = os.getenv(
            'TARGET_EQUITY_FLOW_ENABLED', 'true'
        ).strip().lower() in ('true', '1', 'yes', 'on')
        # Never poll faster than the portfolio cache TTL allows changes to show.
        flow.poll_seconds = max(15, _non_negative_int(
            'TARGET_EQUITY_FLOW_POLL_SECONDS', flow.poll_seconds
        ))
        flow.min_usd = _non_negative_float(
            'TARGET_EQUITY_FLOW_MIN_USD', flow.min_usd
        )
        flow.min_pct = _non_negative_float(
            'TARGET_EQUITY_FLOW_MIN_PCT', flow.min_pct
        )
        flow.max_position_change_pct = min(
            100.0,
            _non_negative_float(
                'TARGET_EQUITY_FLOW_MAX_POSITION_CHANGE_PCT',
                flow.max_position_change_pct,
            ),
        )
        flow.cooldown_seconds = _non_negative_int(
            'TARGET_EQUITY_FLOW_COOLDOWN_SECONDS', flow.cooldown_seconds
        )

        # Blocked assets
        blocked = os.getenv('BLOCKED_ASSETS', '')
        settings.copy_rules.blocked_assets = [
            asset.strip().upper() for asset in blocked.split(',') if asset.strip()
        ]
        
        settings.telegram.bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        settings.telegram.chat_id = os.getenv('TELEGRAM_CHAT_ID')
        
        settings.log_level = os.getenv('LOG_LEVEL', settings.log_level)
        settings.log_file = os.getenv('LOG_FILE', settings.log_file)
        settings.database_url = os.getenv('DATABASE_URL', settings.database_url)
        
        return settings

# Global settings instance
settings = Settings.load()
