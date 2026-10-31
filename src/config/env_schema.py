"""Single source of truth for every environment variable the bot reads.

Why this file exists
--------------------
``.env`` and ``.env.example`` used to be hand-maintained, so they drifted from
the code: options were added to ``settings.py`` and never documented, and
options were removed from ``settings.py`` while still sitting in a user's
``.env`` looking authoritative.  Both failures are invisible at runtime -- the
bot simply uses a default, or ignores a line the user thinks is in effect.

Everything is declared here once; ``env_sync.py`` compares that declaration
against the two dotenv files on every build/start and repairs the difference.
This module must stay dependency-free (stdlib only) so the sync tool can run in
a bare ``python:3.12-slim`` image during ``docker build``.

Field semantics
---------------
``default``  Value the code falls back to when the variable is absent.  New
             keys are appended with this value, so adding a line never changes
             behaviour for an existing deployment.
``sample``   Value written to ``.env.example`` when it differs from ``default``
             (a friendlier starting point that the user is expected to edit).
``scope``    ``runtime``  -> belongs in both ``.env`` and ``.env.example``.
             ``build``    -> read by docker build/compose, documented in
                             ``.env.example`` only.
             ``internal`` -> injected by tooling or a one-off test script; never
                             written, never removed (the audit only needs it to
                             know the name is legitimate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

SCOPE_RUNTIME = "runtime"
SCOPE_BUILD = "build"
SCOPE_INTERNAL = "internal"


@dataclass(frozen=True)
class EnvVar:
    key: str
    default: str
    section: str
    comment: str = ""
    scope: str = SCOPE_RUNTIME
    secret: bool = False
    sample: Optional[str] = None

    @property
    def example_value(self) -> str:
        return self.default if self.sample is None else self.sample

    @property
    def documented(self) -> bool:
        """True when the key belongs in the dotenv files."""
        return self.scope in (SCOPE_RUNTIME, SCOPE_BUILD)


# (title, blurb) -- order here is the order used in .env.example.
SECTIONS: tuple[tuple[str, str], ...] = (
    ("Hyperliquid API", "交易所接口、跟随钱包凭据与账户模式"),
    ("目标账户", "要跟随的地址：普通钱包或 Vault"),
    ("交易模式", "模拟盘 / 实盘切换，首次使用请保持模拟"),
    ("已有仓位", "启动是否自动追入目标当前持仓（默认关闭，改用手动命令）"),
    ("跟单规则", "挂单镜像、下单方式、滑点与合并窗口"),
    ("风控与额度", "名义金额、保证金占用、仓位数上限、币种黑名单"),
    ("Telegram", "通知与命令机器人（可选）"),
    ("数据库与日志", "持久化路径与日志级别"),
    ("高级", "故障排查用，一般无需修改"),
    ("构建参数", "由 docker build / docker compose 读取，不进入运行时配置"),
)

ENV_VARS: tuple[EnvVar, ...] = (
    # ---------------------------------------------------------------- API
    EnvVar(
        "HYPERLIQUID_API_URL",
        "https://api.hyperliquid.xyz",
        "Hyperliquid API",
        "REST 接口地址，主网无需修改。",
    ),
    EnvVar(
        "HYPERLIQUID_LEADERBOARD_URL",
        "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
        "Hyperliquid API",
        "公开收益排行榜接口，通常无需修改。",
    ),
    EnvVar(
        "HYPERLIQUID_WALLET_ADDRESS",
        "",
        "Hyperliquid API",
        "跟单账户（你自己的）钱包地址，用于查持仓与余额。",
    ),
    EnvVar(
        "HYPERLIQUID_PRIVATE_KEY",
        "",
        "Hyperliquid API",
        "跟单账户私钥，仅本机/本容器可见，切勿提交到仓库或发给他人。",
        secret=True,
    ),
    EnvVar(
        "HYPERLIQUID_ACCOUNT_MODE",
        "standard",
        "Hyperliquid API",
        "跟随钱包的账户模式：standard 或 unified。目标地址无需设置模式。",
    ),
    # ------------------------------------------------------------- TARGET
    EnvVar(
        "TARGET_WALLET_ADDRESS",
        "",
        "目标账户",
        "要跟随的目标地址。钱包示例 0x0ba5de43fa2419a25c2e680f84aff3a8f57fce22，\n"
        "# Vault 示例 0xd6e56265890b76413d1d527eb9b75e334c0c5b42。",
    ),
    # ---------------------------------------------------------------- MODE
    EnvVar(
        "SIMULATED_TRADING",
        "true",
        "交易模式",
        "true 为模拟盘（不下真单）；实盘必须改为 false。",
    ),
    EnvVar(
        "SIMULATED_ACCOUNT_BALANCE",
        "1000.0",
        "交易模式",
        "模拟盘初始资金，仅模拟模式生效。",
        sample="10000.0",
    ),
    # ---------------------------------------------------- EXISTING POSITIONS
    EnvVar(
        "COPY_OPEN_POSITIONS",
        "false",
        "已有仓位",
        "历史兼容开关，置 true 也不会下单，仅启动日志提示。\n"
        "# 目标已有仓位一律手动处理：Telegram /copyloss 选择仓位后按市价买入。",
    ),
    EnvVar(
        "COPY_OPEN_PNL_FILTER",
        "loss",
        "已有仓位",
        "/copyloss 不带参数时列出的范围：\n"
        "#   loss   只列目标当前浮亏的仓位（你会在比目标更好的价位进场）\n"
        "#   profit 只列目标当前浮盈的仓位（等于追高，慎用）\n"
        "#   all    全部持仓",
    ),
    EnvVar(
        "COPY_DUPLICATE_GUARD_MINUTES",
        "30",
        "已有仓位",
        "同一币种同方向在复制后多少分钟内不再重复买（0 = 关闭）。\n"
        "# /copyloss ... force 可单次绕过。",
    ),
    # --------------------------------------------------------- COPY RULES
    EnvVar(
        "COPY_EXISTING_ORDERS",
        "true",
        "跟单规则",
        "启动时是否镜像目标当前挂单。实盘建议 false，先观察再开。",
        sample="false",
    ),
    EnvVar(
        "COPY_PENDING_ORDERS",
        "true",
        "跟单规则",
        "目标新挂单出现时是否立即镜像。",
    ),
    EnvVar(
        "CANCEL_MIRRORED_ORDERS",
        "true",
        "跟单规则",
        "目标撤单/过期/被拒时，是否同步撤销跟单侧镜像单。",
    ),
    EnvVar(
        "MIRROR_ORDER_PRICE",
        "true",
        "跟单规则",
        "镜像挂单用目标限价（true）还是当前中间价（false）。",
    ),
    EnvVar(
        "AUTO_ADJUST_SIZE",
        "true",
        "跟单规则",
        "按跟单账户资金规模自动缩放下单量。",
    ),
    EnvVar(
        "USE_LIMIT_ORDERS",
        "false",
        "跟单规则",
        "false：带滑点保护的 IOC 市价单；true：限价单。",
    ),
    EnvVar(
        "MAX_SLIPPAGE_PCT",
        "1.0",
        "跟单规则",
        "IOC 市价单允许的价格偏离百分比，超过则拒单。",
    ),
    EnvVar(
        "MAX_MARGIN_USAGE_RATIO",
        "0.40",
        "跟单规则",
        "每笔开仓最多使用可用保证金的比例（0.05–0.95），HIP-3 标的建议保守。",
    ),
    EnvVar(
        "PENDING_ORDER_MERGE_WINDOW_SECONDS",
        "5",
        "跟单规则",
        "同币种、同方向的密集目标挂单在此静默窗口内合并为一笔镜像单。",
    ),
    EnvVar(
        "LEVERAGE_ADJUSTMENT",
        "0.5",
        "跟单规则",
        "0.5：使用目标杠杆的一半；1.0：完全跟随目标杠杆。",
    ),
    # ---------------------------------------------------------------- RISK
    EnvVar(
        "MAX_POSITION_SIZE",
        "x",
        "风控与额度",
        "单仓名义价值上限（美元）；x 表示不限制，只受交易所可用保证金约束。",
    ),
    EnvVar(
        "MAX_TOTAL_EXPOSURE",
        "x",
        "风控与额度",
        "全部仓位合计名义价值上限；x 表示不限制。",
    ),
    EnvVar(
        "MAX_OPEN_TRADES",
        "x",
        "风控与额度",
        "同时跟随的仓位数；x 表示不限制，填 1 只跟第一个仓位。",
    ),
    EnvVar(
        "MAX_OPEN_ORDERS",
        "x",
        "风控与额度",
        "同时存在的跟随挂单数上限；x 表示不限制。",
    ),
    EnvVar(
        "MAX_ACCOUNT_EQUITY",
        "x",
        "风控与额度",
        "账户权益上限（美元），超过后不再开新仓；x 表示不限制。",
    ),
    EnvVar(
        "BLOCKED_ASSETS",
        "",
        "风控与额度",
        "币种黑名单，英文逗号分隔，例如 BTC,ETH,SOL。",
    ),
    # ------------------------------------------------------------ TELEGRAM
    EnvVar(
        "TELEGRAM_BOT_TOKEN",
        "",
        "Telegram",
        "机器人 Token（@BotFather 获取）。留空则不启用 Telegram。",
        secret=True,
    ),
    EnvVar(
        "TELEGRAM_CHAT_ID",
        "",
        "Telegram",
        "接收通知的会话 ID；/start 会在日志里打印你的 chat id。",
    ),
    # -------------------------------------------------------------- STORAGE
    EnvVar("DATABASE_URL", "sqlite:///./data/trading.db", "数据库与日志", "交易记录数据库。"),
    EnvVar("LOG_LEVEL", "INFO", "数据库与日志", "DEBUG / INFO / WARNING / ERROR。"),
    EnvVar("LOG_FILE", "./logs/trading.log", "数据库与日志", "日志文件路径。"),
    # ------------------------------------------------------------ ADVANCED
    EnvVar(
        "FILL_POLLING_ENABLED",
        "false",
        "高级",
        "REST 成交补漏，WebSocket 断线期间靠它补查成交，默认关闭。",
    ),
    EnvVar(
        "FILL_POLL_INTERVAL_SECONDS",
        "15",
        "高级",
        "成交补漏轮询间隔（秒），小于 15 会被抬到 15。",
    ),
    EnvVar(
        "ASSET_METADATA_TTL_SECONDS",
        "300",
        "高级",
        "未知币种的负缓存时长（秒，最小 30）；已知标的走内存不重复拉取。",
    ),
    EnvVar(
        "COPY_LEDGER_PATH",
        "",
        "高级",
        "手动复制账本路径，用于防重复下单；留空为 data/copyloss_ledger.json。",
    ),
    # --------------------------------------------------------------- BUILD
    EnvVar(
        "INSTALL_TELEGRAM",
        "true",
        "构建参数",
        "构建时是否安装 Telegram 依赖；不用 Telegram 可设 false 缩减镜像。",
        scope=SCOPE_BUILD,
    ),
    EnvVar(
        "BUILD_ID",
        "local",
        "构建参数",
        "构建时注入的 commit 短哈希，/start 与 /status 的版本行会显示它。",
        scope=SCOPE_INTERNAL,
    ),
    EnvVar(
        "LIVE_ORDER_TEST_CONFIRM",
        "",
        "构建参数",
        "仅 src/verify_*.py 真实下单自检用，运行机器人时无作用。",
        scope=SCOPE_INTERNAL,
    ),
)

_BY_KEY: dict[str, EnvVar] = {var.key: var for var in ENV_VARS}
_SECTION_ORDER: dict[str, int] = {title: i for i, (title, _) in enumerate(SECTIONS)}

# Variables that belong to the tooling around the bot rather than to the bot.
# docker compose reads COMPOSE_*/DOCKER_* straight out of .env, so deleting a
# "stale" key here would break the deployment; they are never removed, and
# never added.
FOREIGN_PREFIXES: tuple[str, ...] = ("COMPOSE_", "DOCKER_")
FOREIGN_KEYS: frozenset[str] = frozenset({"TZ", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE"})


def is_foreign(key: str) -> bool:
    return key in FOREIGN_KEYS or key.startswith(FOREIGN_PREFIXES)

HEADER = """\
# =====================================================================
# Hyperliquid 跟单机器人 配置样例
#
# 本文件由 src/config/env_schema.py 生成，请勿手工编辑：
#   python src/config/env_sync.py --example      重新生成
#   python src/config/env_sync.py --apply        把已有 .env 补齐/清理
#
# 每次 docker build 都会校验本文件与代码是否一致（--check-example），
# 每次容器启动都会把 .env 与代码对齐（新增项补上、失效项移除）。
# ====================================================================="""


def get(key: str) -> Optional[EnvVar]:
    return _BY_KEY.get(key)


def keys(include_internal: bool = True) -> set[str]:
    return {
        var.key
        for var in ENV_VARS
        if include_internal or var.scope != SCOPE_INTERNAL
    }


def documented_vars() -> list[EnvVar]:
    """Runtime + build variables, in .env.example order."""
    ordered = sorted(
        (var for var in ENV_VARS if var.documented),
        key=lambda var: _SECTION_ORDER.get(var.section, 999),
    )
    return ordered


def section_blurb(section: str) -> str:
    for title, blurb in SECTIONS:
        if title == section:
            return blurb
    return ""


def render_example() -> str:
    """Render .env.example from the schema. Deterministic, no timestamps."""
    chunks: list[str] = [HEADER, ""]
    current_section: Optional[str] = None
    for var in documented_vars():
        if var.section != current_section:
            if current_section is not None:
                chunks.append("")
            current_section = var.section
            rule = "# " + "-" * 69
            chunks.append(rule)
            title = f"# {var.section}"
            blurb = section_blurb(var.section)
            if blurb:
                title += f" —— {blurb}"
            chunks.append(title)
            chunks.append(rule)
        if var.comment:
            chunks.extend(_comment_lines(var.comment))
        chunks.append(f"{var.key}={var.example_value}")
    chunks.append("")
    return "\n".join(chunks)


def _comment_lines(comment: str) -> list[str]:
    """Split a comment into '#'-prefixed lines, honouring embedded breaks."""
    out: list[str] = []
    for raw in comment.split("\n"):
        text = raw.strip()
        if text.startswith("#"):
            out.append(text)
        else:
            out.append(f"# {text}" if text else "#")
    return out
