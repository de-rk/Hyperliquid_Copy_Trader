# Hyperliquid 跟单机器人

基于 Hyperliquid WebSocket 的跟单程序。它监听指定目标钱包的**新成交**，按跟随钱包与目标钱包的资金比例计算数量，并在 Hyperliquid 永续合约账户中提交对应订单。

> 风险提示：这是交易执行程序，不保证盈利。先使用模拟模式验证，再用少量资金开启实盘。私钥只应保存在本机或服务器的 `.env`，不要提交、截图或发送给任何人。

## 当前行为

- 只复制机器人启动后的目标**成交**，避免复制尚未成交的挂单。
- `COPY_OPEN_POSITIONS=false` 时，不会在启动时追入目标已有仓位。
- 一笔目标订单分多次成交时，程序按每个 `fill.sz` 分别计算跟随数量，不会重复复制整个目标仓位。
- 平仓仅在跟随钱包存在同方向仓位时执行，并使用 `reduce-only`，不会反手开仓。
- 单笔名义价值低于 `$10` 会跳过，这是 Hyperliquid 的最低订单要求。
- 新开仓会限制在可用保证金的 95% 以内，并遵守 `MAX_OPEN_TRADES`。
- WebSocket 之外每 15 秒检查一次新 fills，用于补回连接重连期间漏掉的事件；启动前的历史 fills 仅作为基线，不会自动补单。
- 容器日志和 Telegram 通知使用中国标准时间（UTC+8）。

如果机器人启动前目标已经开仓，且需要主动复制该已有仓位，必须由使用者明确设置 `COPY_OPEN_POSITIONS=true` 并重启服务。该设置会产生真实订单；默认 `false` 不会追入已有仓位。

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`。首次使用请保留：

```properties
SIMULATED_TRADING=true
COPY_OPEN_POSITIONS=false
COPY_EXISTING_ORDERS=false
AUTO_ADJUST_SIZE=true
LEVERAGE_ADJUSTMENT=0.5
MAX_OPEN_TRADES=1
```

必须填写：

```properties
HYPERLIQUID_WALLET_ADDRESS=0x你的跟随钱包地址
HYPERLIQUID_PRIVATE_KEY=0x该钱包的私钥
TARGET_WALLET_ADDRESS=0x要跟随的目标地址
```

`HYPERLIQUID_WALLET_ADDRESS` 必须与私钥推导出的地址一致。用于实盘的 USDC 必须位于 Hyperliquid 的 **Perp** 账户，不是 Spot 账户。

### 2. 启动 Docker 服务

```bash
docker compose up -d --build
docker compose logs -f --tail=100 copy-trader
```

停止服务：

```bash
docker compose down
```

代码更新后必须使用 `up -d --build` 重新构建；仅执行 `restart` 不会更新镜像内的代码。

### 3. 切换到实盘前的检查

先在模拟模式观察新成交。看到以下日志，说明监听与尺寸计算正常：

```text
Fill copied: xyz:SKHX buy size=... target_size=... reduce_only=False
```

确认后将 `.env` 改为：

```properties
SIMULATED_TRADING=false
```

再重新创建容器：

```bash
docker compose down
docker compose up -d --build
```

## 配置说明

```properties
# Hyperliquid API
HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
# 公开排行榜接口（通常无需修改）
HYPERLIQUID_LEADERBOARD_URL=https://stats-data.hyperliquid.xyz/Mainnet/leaderboard

# 跟随钱包。实盘时两个值必须填写且地址必须匹配。
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_PRIVATE_KEY=

# 目标钱包或 Vault 地址
TARGET_WALLET_ADDRESS=

# true 为模拟模式；实盘必须明确改为 false
SIMULATED_TRADING=true
SIMULATED_ACCOUNT_BALANCE=1000.0

# 启动时是否复制目标已有仓位。建议保持 false。
COPY_OPEN_POSITIONS=false

# 启动时是否复制目标已有挂单。建议保持 false。
COPY_EXISTING_ORDERS=false

# true 时，单笔跟随数量 = 目标本次成交量 × 跟随资金 / 目标资金
AUTO_ADJUST_SIZE=true

# false 为带滑点保护的 IOC 市价单；true 为限价单。
USE_LIMIT_ORDERS=false
MAX_SLIPPAGE_PCT=1.0

# 目标杠杆的倍数。0.5 表示目标 6x 时使用 3x。
LEVERAGE_ADJUSTMENT=0.5

# x 表示不限制。建议实盘先用 1。
MAX_OPEN_TRADES=1
MAX_OPEN_ORDERS=x
MAX_ACCOUNT_EQUITY=x

# 不跟随的币种，使用英文逗号分隔
BLOCKED_ASSETS=

# Telegram 为可选功能
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
INSTALL_TELEGRAM=true
```

### 资金与仓位示例

假设目标账户 `$100,000`，跟随账户 `$1,000`，目标本次成交 `10` 个币：

- 资金比例为 `1%`
- 跟随数量为 `0.1` 个币
- 程序检查该数量的名义价值是否至少 `$10`
- 程序再检查该订单所需保证金是否不超过跟随账户可用保证金的 95%

启动日志中的 `Your Copy` 是“若复制目标当前全部旧仓位”的预估，不代表已下单。只有 `COPY_OPEN_POSITIONS=true` 才会在启动时执行该操作。

## Telegram

填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 后，机器人会发送中文通知，并支持：

- `/status` 查看运行状态和跟随账户信息
- `/positions` 查看当前仓位
- `/orders` 查看挂单
- `/pnl` 查看收益摘要
- `/leaderboard` 查看 Hyperliquid 公开排行榜，可按 24H、7D、30D 的收益额或收益率筛选；最多读取 200 名，每页 10 名
- `/wallet 0x地址 [1-20]` 查询任意公开地址的账户状态、24H/7D/30D 净值变化和最近成交；默认显示 10 笔
- `/pause` 暂停复制新成交，保留已有仓位
- `/resume` 恢复复制
- `/stop` 停止机器人，可选择是否平仓

同一个 Telegram Token 只能由一个实例轮询。出现 `terminated by other getUpdates request` 时，关闭使用同一 Token 的其他机器人实例。

`/pnl` 会分别显示目标钱包和跟随钱包。实盘模式下，24H、7D、30D 使用账户净值历史计算；充值或提现也会影响净值变化。模拟模式没有跟随钱包的链上历史，因此这些周期显示为暂无数据。

### 排行榜与地址查询

发送 `/leaderboard` 后，使用 Telegram 按钮选择 `24H`、`7D` 或 `30D`，并按“收益额”或“收益率”排序。机器人最多读取公开排行榜前 200 名，每页展示 10 个完整钱包地址，可用“上一页 / 下一页”翻页。

使用 `/wallet` 可查询任意公开 Hyperliquid 地址，例如：

```text
/wallet 0x85ecf584f25db6f146718b86d493e33c5af72052
/wallet 0x85ecf584f25db6f146718b86d493e33c5af72052 20
```

第二个参数可选，范围为 `1-20`，表示展示最近多少笔成交，默认 `10`。查询结果包含：

- 当前账户价值、未实现盈亏、持仓数和挂单数
- 24H、7D、30D 账户净值变化及收益率
- 最近成交（交易所 fills）的方向、数量、价格、UTC 时间、已实现盈亏和手续费

地址查询和排行榜仅使用 Hyperliquid 公开数据，不需要私钥，也不会发出交易。账户净值变化会受充值和提现影响；“最近成交”是交易所返回的成交明细，可能将一笔下单拆分成多条，不等同于按开仓和平仓配对后的完整交易。

## 常见问题

### `User or API Wallet ... does not exist`

这通常是旧版本订单签名格式与交易所不一致造成的。拉取本仓库最新代码后执行：

```bash
docker compose down
docker compose up -d --build
```

如果仍出现该错误，检查 `HYPERLIQUID_WALLET_ADDRESS` 是否确实由 `HYPERLIQUID_PRIVATE_KEY` 推导而来。

### 只收到小时报告，没有跟单

检查目标是否真的有新的 `Open` 或 `Add` 成交；`Close` 或 `Reduce` 成交只有在跟随账户已有相同仓位时才会发送平仓单。用以下命令查看相关日志：

```bash
docker compose logs --since 24h copy-trader | grep -E 'FILL DETECTED|Fill copied|Failed to copy fill|Hyperliquid rejected'
```

### 余额显示为 0

请将 USDC 从 Hyperliquid 的 Spot 账户转入 Perp 账户。实盘下单和仓位保证金只使用 Perp 余额。

## 本地运行

需要 Python 3.12：

```bash
pip install -r requirements.txt
pip install -r requirements-telegram.txt  # 仅在使用 Telegram 时需要
python src/main.py
```

## 免责声明

数字资产交易可能导致全部本金损失。软件按“现状”提供，不提供任何收益、可用性或适用性保证。使用者自行承担所有交易、配置和密钥管理风险。
