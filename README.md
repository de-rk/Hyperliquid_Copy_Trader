# Hyperliquid 跟单机器人

基于 Hyperliquid WebSocket 的跟单程序。它监听指定目标钱包的**新挂单和成交**，按跟随钱包与目标钱包的资金比例计算数量，并在 Hyperliquid 永续合约账户中提交对应订单。

> 风险提示：这是交易执行程序，不保证盈利。先使用模拟模式验证，再用少量资金开启实盘。私钥只应保存在本机或服务器的 `.env`，不要提交、截图或发送给任何人。

## 当前行为

- 默认镜像机器人启动后的目标**限价挂单**：目标挂单后立即按同一价格创建跟随挂单；目标取消、过期或被拒时，撤销仍未成交的跟随挂单。
- 目标挂单成交时不会再次创建跟随订单；跟随挂单自身的成交负责改变跟随仓位。若目标订单部分成交，跟随挂单保留剩余数量。
- **启动时绝不复制目标已有仓位**，一次下单都不会发。已有仓位只能在 Telegram 用 `/copyloss` 手动处理：先列编号清单，可用序号只挑其中几笔，执行后回执每笔的成交价、数量、名义额和订单号。`COPY_OPEN_POSITIONS` 仅为兼容保留，置 `true` 也不会下单，只会多打一条提示。
- `/copyloss` 默认列出目标**当前浮亏**的仓位（`COPY_OPEN_PNL_FILTER=loss`，你在更好的价位进场）；`/copyloss all` 列全部、`/copyloss profit` 只列浮盈。筛选按交易所返回的 `unrealizedPnl` 判断，恰好持平的不算浮亏。
- **每次下单前跑三重重复检查**：跟随钱包已持有同方向、持有反方向、以及同一币种在 `COPY_DUPLICATE_GUARD_MINUTES`（默认 30）分钟内刚复制过。三者在预览里就标出来，`confirm` 时再按下单那一刻的实盘持仓复核一次；读不到跟随钱包持仓时直接拒单，不会“当作空仓”继续买。确需加仓用 `force` 显式绕过（反方向持仓不受 `force` 影响）。
- 一笔目标订单分多次成交时，程序按每个 `fill.sz` 分别计算跟随数量，不会重复复制整个目标仓位。
- 平仓按目标本次减仓占其平仓前仓位的比例执行；仅在跟随钱包存在同方向仓位时使用 `reduce-only`，不会反手开仓。
- 单笔名义价值低于 `$10` 会跳过，这是 Hyperliquid 的最低订单要求。
- 新开仓会按跟随钱包的账户模式读取市场可交易额度，再限制在 `MAX_MARGIN_USAGE_RATIO`（默认 40%）以内。
- `MAX_POSITION_SIZE`、`MAX_TOTAL_EXPOSURE`、`MAX_OPEN_TRADES` 默认都是 `x`（不限制），目标持几个仓位就跟几个。此时仓位大小只由可交易额度与 `MAX_MARGIN_USAGE_RATIO` 决定，详见[资金与仓位示例](#资金与仓位示例)。
- 杠杆按交易所 `meta` 中该标的的 `maxLeverage` 截断，`onlyIsolated` 标的自动使用逐仓。不会向低杠杆标的（如 `flx:BTC` 1x、`GMX` 3x）发送必被拒绝的杠杆值。
- 只复制永续成交。现货成交会被跳过，包括交易所自动把残渣兑换成 USDC 产生的 `Spot Dust Conversion`，这类成交只在日志里记一条 INFO，不影响仓位。
- 资产表在进程内缓存：遇到未知币种时刷新一次，之后在 `ASSET_METADATA_TTL_SECONDS`（默认 300 秒）内不再重复请求。新上架的 HIP-3 标的无需重启即可解析。
- 默认只使用 WebSocket 监听成交；`FILL_POLLING_ENABLED=true` 时才会按 `FILL_POLL_INTERVAL_SECONDS`（默认 15 秒）查询 REST 接口补回断线期间漏掉的成交。启动前的历史 fills 仅作为基线，不会自动补单。
- 每小时报告的统计块首行是跟单状态（`🟢 正在跟单` / `⏸️ 暂停跟单，自 MM-DD HH:MM 起` / `🔴 已停止`）。暂停期间仍按时发报，但报告会明确标出已暂停多久。
- 容器日志和 Telegram 通知使用中国标准时间（UTC+8）。

目标在机器人启动前就持有的仓位，启动时**不会**被追入。原因是这条路径曾经在后台直接下真实市价单，既没有预览也没有通知，只能事后在交易所账本里发现。现在它被 `/copyloss` 取代：

```text
/copyloss                 # 列出目标当前浮亏的仓位（带编号），不下单
/copyloss 1 3             # 预览只买第 1、3 笔
/copyloss 1 3 confirm     # 真正下单，随后返回逐笔回执
/copyloss confirm         # 清单里的浮亏仓位全部买
```

比例、杠杆截断、额度上限都按**下单那一刻**的双端净值和市价现算。

重复买同一笔是这条路径最容易出的问题，所以下单前有三道检查，任何一道命中都不下单：

| 检查 | 命中时 | `force` 能否绕过 |
|---|---|---|
| 跟随钱包已持有同方向 | 跳过，避免重复加仓 | 可以（明确加仓） |
| 跟随钱包持有反方向 | 跳过，避免先平掉/反手 | **不行**，先平掉旧仓 |
| 同币种同方向在窗口内（默认 30 分钟）复制过 | 跳过，等交易所同步或调窗口 | 可以 |

第三条是独立于持仓查询的：市价单成交后交易所需要一点时间才会在持仓里显示，仅靠“有没有持仓”判断，双击一次或重启一次就会再买一遍。这条记录写在 `data/copyloss_ledger.json`（容器里已挂载 `./data`），进程重启后仍然有效；模拟单和实盘单分开记，跑模拟不会挡住实盘。

## 快速开始

### 1. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`。首次使用请保留：

```properties
SIMULATED_TRADING=true
COPY_OPEN_POSITIONS=false
COPY_OPEN_PNL_FILTER=loss
COPY_EXISTING_ORDERS=false
COPY_PENDING_ORDERS=true
CANCEL_MIRRORED_ORDERS=true
MIRROR_ORDER_PRICE=true
AUTO_ADJUST_SIZE=true
LEVERAGE_ADJUSTMENT=0.5
MAX_OPEN_TRADES=x
```

必须填写：

```properties
HYPERLIQUID_WALLET_ADDRESS=0x你的跟随钱包地址
HYPERLIQUID_PRIVATE_KEY=0x该钱包的私钥
HYPERLIQUID_ACCOUNT_MODE=standard
TARGET_WALLET_ADDRESS=0x要跟随的目标地址
```

`HYPERLIQUID_WALLET_ADDRESS` 必须填写由 `HYPERLIQUID_PRIVATE_KEY` 推导出的钱包地址（当前程序要求两者匹配）。`HYPERLIQUID_ACCOUNT_MODE` 要与跟随账户在 Hyperliquid 中的模式一致：

- `standard`：现货、默认 Perp 和各 HIP-3 Perp DEX 的保证金分开。资金须位于该市场可用的 Perp 账户/DEX。
- `unified`：同一抵押币种的余额在现货与支持该币种的 Perp 市场间统一。程序使用 `activeAssetData` 读取当前市场、交易方向的可交易额度。不同抵押币种不会自动互换或合并。

目标地址可以是 Standard 或 Unified；跟单比例使用账户级 `portfolio` 净值。目标持仓状态和目标账户净值分开读取。

#### 1.1 `.env` 会自动与代码对齐

`.env` 和 `.env.example` 不再靠手工维护：所有环境变量在 `src/config/env_schema.py` 里声明一次，`.env.example` 由它生成，`.env` 在每次构建/启动时自动对齐。

| 情况 | 处理 |
|---|---|
| 代码新增、`.env` 里没有 | 追加到文件末尾，值取**代码默认值**（等于没配，行为不变） |
| `.env` 里有、代码已不再读取 | 删除该行（改动前整份备份到 `data/env_backups/`） |
| 两边都有 | **完全不动**，即使值为空或与默认值不同 |
| 同一个键出现多次 | 保留第一次出现的值，删掉后面的 |
| 被注释掉的键（`#FOO=1`） | 视为未设置，会被追加成真实键 |
| `COMPOSE_*`、`DOCKER_*`、`TZ` 等外围变量 | 原样保留，既不删也不加 |

同步发生在两个位置，任选其一都会生效：`linux/start.sh`、`windows/start.bat` 在构建前跑一次；容器 CMD 在 `python src/main.py` 之前再跑一次（`.env` 是挂载进去的，容器能直接改宿主机上的文件）。手动执行：

```bash
python src/config/env_sync.py            # 只看差异，不写文件
python src/config/env_sync.py --apply    # 修复 .env
python src/config/env_sync.py --example  # 由 schema 重新生成 .env.example
```

加一个新配置的正确顺序：在 `env_schema.py` 里加一条 → `--example` 重新生成 `.env.example` → 一起提交。即使漏了中间那步也不会静默出错：`docker build` 里有 `--audit` 和 `--check-example` 两道门禁，代码读了没声明的变量、或样例与 schema 不一致，构建直接失败。这道门禁是必要的——否则新变量会被同步逻辑当成"失效配置"从用户的 `.env` 里删掉。

另外，`docker-compose.yml` 的 `environment:` 不再硬编码业务开关。compose 的 `environment` 优先级高于 `.env`，之前写死的 `FILL_POLLING_ENABLED=false` 会让 `.env` 里同名的配置静默失效。

### 2. 启动 Docker 服务

```bash
docker compose up -d --build
docker compose logs -f --tail=100 copy-trader
```

停止服务：

```bash
docker compose down
```

代码更新后必须使用 `up -d --build` 重新构建；仅执行 `restart` 不会更新镜像内的代码。`linux/start.sh` 与 `windows/start.bat` 已带 `--build`，并把当前 commit 作为 `BUILD_ID` 注入镜像。

判断线上到底跑的是哪一版：在 Telegram 发 `/start`，最后一行是 `版本：v1.1.0 +<commit>`。**没有这一行，就说明容器里还是旧镜像**——旧版本的帮助文本里也没有 `/copyloss`，这正是"命令加了但看不到"的典型症状。

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

### 4. 验证真实下单线路

若要验证签名、下单、成交查询和 `reduce-only` 平仓，可运行一次 ETH 验证脚本。它只能在实盘模式、默认 Perp DEX 没有 ETH 仓位、且传入确认词时执行；开仓请求固定在 `$10-$12`，随后立即按交易所报告的实际仓位平仓。

```bash
docker compose stop copy-trader
docker compose run --rm -e LIVE_ORDER_TEST_CONFIRM=ETH_12_USD copy-trader \
  python src/verify_eth_live_order.py
docker compose up -d copy-trader
```

此命令产生两笔真实 ETH 市价单的手续费和极小滑点。若脚本在开仓后报错，先在 Hyperliquid 界面检查 ETH 仓位，确认平仓后再启动机器人。

### 5. HIP-3（builder DEX）标的

目标钱包在 `xyz`、`flx`、`vntl` 等 builder DEX 上的仓位现在也会被跟踪。这类标的在接口里始终是 `{dex}:{coin}` 形式（例如 `xyz:SKHX`），大小写敏感：`xyz:SKHX` 能查到 `availableToTrade`，写成 `XYZ:SKHX` 会被交易所返回 500 或 `null`。程序内部只在 DEX 段做小写归一化，不会改写 ticker。

下单时不需要额外的 DEX 字段，builder DEX 由资产编号编码：

```
asset = 100000 + perp_dex_index × 10000 + index_in_meta
```

`perpDexs[0]` 是默认 Perp DEX，`xyz` 是 `perpDexs[1]`，所以 `xyz:SKHX` 的编号是 `100000 + 1 × 10000 + 22 = 110022`。

builder DEX 的杠杆上限普遍低于默认 Perp（默认 Perp 的 `BTC` 是 40x）：`xyz:SKHX` 是 10x，`GMX`、`SNX` 只有 3x，`flx:BTC` 只有 1x 且强制逐仓。程序启动时并发读取全部 DEX 的 `meta`（11 个 DEX、553 个标的，约 0.4 秒），下单前按实际上限截断杠杆，`onlyIsolated` 标的自动切成逐仓，因此不需要手动维护杠杆表。新上架的标的最迟会在一次未知币种刷新后被识别，不必重启容器。

先跑只读诊断，确认解析、DEX 状态、`availableToTrade` 和将要发送的订单体：

```bash
docker compose run --rm copy-trader python src/verify_hip3_order.py xyz:SKHX
```

需要真实下单验证时再传确认词，行为与 ETH 脚本一致（上限 `$12`，随后立即 `reduce-only` 平仓）：

```bash
docker compose run --rm -e LIVE_ORDER_TEST_CONFIRM=HIP3_12_USD copy-trader \
  python src/verify_hip3_order.py xyz:SKHX
```

Unified 账户下额度取 `activeAssetData` 的方向额度，而不是某个 DEX 的 `accountValue`；`xyz` DEX 的抵押币种是 USDC，与现货共用同一份保证金。

## 配置说明

```properties
# Hyperliquid API
HYPERLIQUID_API_URL=https://api.hyperliquid.xyz
# 公开排行榜接口（通常无需修改）
HYPERLIQUID_LEADERBOARD_URL=https://stats-data.hyperliquid.xyz/Mainnet/leaderboard

# 跟随钱包。实盘时两个值必须填写且地址必须匹配。
HYPERLIQUID_WALLET_ADDRESS=
HYPERLIQUID_PRIVATE_KEY=
# 跟随钱包模式：standard 或 unified，必须与 Hyperliquid 账户设置一致。
HYPERLIQUID_ACCOUNT_MODE=standard

# 目标钱包或 Vault 地址
TARGET_WALLET_ADDRESS=

# true 为模拟模式；实盘必须明确改为 false
SIMULATED_TRADING=true
SIMULATED_ACCOUNT_BALANCE=1000.0

# 启动时不复制已有仓位，此项仅为兼容保留（置 true 也不会下单）。
COPY_OPEN_POSITIONS=false
# /copyloss 不带参数时列出的范围：
#   loss   目标当前浮亏的仓位：你在比目标更好的价位进场，等于“接目标的水下仓”
#   profit 目标当前浮盈的仓位：等于追高，慎用
#   all    全部持仓
COPY_OPEN_PNL_FILTER=loss
# 同币种同方向在上次手动复制后多少分钟内不再重复买；0 = 关闭该检查。
# /copyloss ... force 可单次绕过。
COPY_DUPLICATE_GUARD_MINUTES=30

# 启动时是否复制目标已有挂单。建议保持 false。
COPY_EXISTING_ORDERS=false

# 是否在目标挂单出现时立即镜像，以及目标取消时联动撤销。
COPY_PENDING_ORDERS=true
CANCEL_MIRRORED_ORDERS=true
# true 使用目标限价；false 使用跟随账户读取的当前中间价。
MIRROR_ORDER_PRICE=true
# 同币种、同方向的密集挂单在该静默窗口（秒）内合并为一笔镜像挂单。
PENDING_ORDER_MERGE_WINDOW_SECONDS=5

# true 时，单笔跟随数量 = 目标本次成交量 × 跟随资金 / 目标资金
AUTO_ADJUST_SIZE=true

# false 为带滑点保护的 IOC 市价单；true 为限价单。
USE_LIMIT_ORDERS=false
MAX_SLIPPAGE_PCT=1.0
# 每笔开仓最多使用可交易额度的比例。Standard 按该 DEX 可用保证金和杠杆估算；Unified 按 activeAssetData 的方向额度限制。
MAX_MARGIN_USAGE_RATIO=0.40
# 默认关闭 REST 成交补漏；需要时改为 true
FILL_POLLING_ENABLED=false
FILL_POLL_INTERVAL_SECONDS=15
# 未知币种的负缓存时长（秒，最小 30）。同时决定新上架标的最迟多久被识别；
# 已知标的始终走内存，不会按该间隔重复拉取资产表。
ASSET_METADATA_TTL_SECONDS=300

# 目标杠杆的倍数。0.5 表示目标 6x 时使用 3x。
LEVERAGE_ADJUSTMENT=0.5

# 单个跟随仓位的名义价值上限。x 表示不限制（默认），此时只受交易所可用保证金约束；
# 填数字则把单仓压到该美元额度。
MAX_POSITION_SIZE=x

# 全部仓位合计名义价值上限。x 表示不限制（默认）；真实风险上限由
# MAX_MARGIN_USAGE_RATIO 对交易所可交易额度的约束决定。
MAX_TOTAL_EXPOSURE=x

# 同时跟随的仓位数上限。x 表示不限制（默认）：目标持几个仓就跟几个。
# 填 1 会只保留第一个仓位，目标同时持 BTC 和 SKHX 时第二个会被直接跳过。
MAX_OPEN_TRADES=x
MAX_OPEN_ORDERS=x
MAX_ACCOUNT_EQUITY=x

# 不跟随的币种，使用英文逗号分隔
BLOCKED_ASSETS=

# Telegram 为可选功能
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
INSTALL_TELEGRAM=true

# 数据库与日志
DATABASE_URL=sqlite:///./data/trading.db
LOG_LEVEL=INFO
LOG_FILE=./logs/trading.log
```

### 资金与仓位示例

假设目标账户 `$100,000`，跟随账户 `$1,000`，目标本次成交 `10` 个币：

- 资金比例为 `1%`
- 跟随数量为 `0.1` 个币
- 程序检查该数量的名义价值是否至少 `$10`
- 程序再检查该订单是否不超过对应账户模式下可交易额度的 `MAX_MARGIN_USAGE_RATIO`（默认 40%）

### 仓位上限一览

| 配置 | 默认 | 作用 |
|---|---|---|
| `MAX_MARGIN_USAGE_RATIO` | `0.40` | **默认唯一生效的规模约束**：单笔开仓最多使用可交易额度的 40% |
| `LEVERAGE_ADJUSTMENT` | `0.5` | 使用目标杠杆的一半，实际值再按交易所上限截断 |
| `MAX_POSITION_SIZE` | `x` | 单个仓位名义价值上限；填数字才生效 |
| `MAX_TOTAL_EXPOSURE` | `x` | 全部仓位合计名义价值上限；填数字才生效 |
| `MAX_OPEN_TRADES` | `x` | 同时跟随的仓位数；填 `1` 只跟第一个仓位 |
| `MAX_ACCOUNT_EQUITY` | `x` | 跟随账户权益达到该值时自动暂停 |
| `MIN_POSITION_SIZE_USD` | `$10` | 代码内常量（交易所最低订单额），低于则跳过并推送 Telegram 通知 |

三个美元额度上限（`MAX_POSITION_SIZE`、`MAX_TOTAL_EXPOSURE`、`MAX_OPEN_TRADES`）默认都是不限制，实际生效的规模约束只有 `MAX_MARGIN_USAGE_RATIO`。这意味着：目标在 `xyz:SKHX` 上持有 `$900,000` 名义、跟随资金是目标的 `1%` 时，会真的按 `$9,000` 下这一笔，而不是被压到某个固定美元额度。这类尺寸完全依赖 `MAX_MARGIN_USAGE_RATIO` 兜底，所以不要为了“多跟一点”把它调到 0.9 以上——目标账户净值读取异常时，它是最后一道闸门。

反过来，如果只想拿小额试水，显式写 `MAX_POSITION_SIZE=200` 比下调 `MAX_MARGIN_USAGE_RATIO` 更合适：前者只压单仓，不影响多仓位同时跟进。

启动日志中的 `Your Copy` 是“若复制目标当前符合筛选的旧仓位”的预估，**不代表已下单**，启动时也不会下这些单。要执行必须到 Telegram 发 `/copyloss`；那里的每一条也会先过重复检查，`Your Copy` 里被标成已持有的不会下单。

日志里的 `PnL Filter: loss -> copying 2/7 position(s)` 只决定 `/copyloss` 默认列出的范围；清单按这个范围编号，可以只挑其中几笔，实际下单时仍会按当时的净值和市价重算数量。

追浮亏仓位的经济含义：跟随钱包永远按**当前市价**成交，不吃目标的历史成本。目标浮亏时，它的入场价位于市价不利的一侧，你在更有利的一侧接同样的方向；反之 `profit` 意味着你比目标买得更贵。这也是为什么 `loss` 是更常见的默认值。

## Telegram

填写 `TELEGRAM_BOT_TOKEN` 和 `TELEGRAM_CHAT_ID` 后，机器人会发送中文通知，并支持：

- `/status` 查看运行状态和跟随账户信息（末尾附带当前的追入旧仓位策略）
- `/positions` 查看当前仓位
- `/orders` 查看挂单
- `/pnl` 查看收益摘要，含目标钱包与跟随钱包的完整地址
- `/leaderboard` 查看 Hyperliquid 公开排行榜，可按 24H、7D、30D 的收益额或收益率筛选；最多读取 200 名，每页 10 名
- `/wallet 0x地址 [1-20]` 查询任意公开地址的账户状态、24H/7D/30D 净值变化和最近成交；默认显示 10 笔
- `/copyloss [loss|all|profit] [序号...] [confirm] [force]` 手动复制目标现有仓位（启动时永不自动复制）：不带参数先列出编号清单和每笔大约要花的钱；`/copyloss 1 3` 只选第 1、3 笔；加 `confirm` 才真正下单，随后返回逐笔回执（成交价、数量、名义额、订单号，失败项带原因）。已持有、持有反方向、或 30 分钟内复制过的会在预览里标出并在下单时拦下；确需叠加同一仓位追加 `force`。
- `/close [序号|币种|all] [confirm]` 平掉**跟随账户自己**的仓位：不带参数列出编号清单（数量、≈金额、未实现盈亏）；可用序号（`/close 1 3 confirm`）、币种片段（`/close BTC confirm`、`/close xyz:SKHX confirm`）或 `all` 选择，加 `confirm` 才发送 reduce-only 市价单，随后返回逐笔回执（平仓方向、成交价、名义额、已实现盈亏、订单号，失败项带原因）。暂停或停止状态下都能用——`/stop` 现在只停跟单不动仓位，平仓完全由这个命令负责。
- `/start` 启动或恢复跟单：暂停后用它恢复；停止后也用它恢复，无需重启容器（恢复时重新连接目标监控，不会补跟停止期间的历史成交）
- `/stop` 停止跟单：取消全部挂单、停止复制，**保留所有仓位**；不再询问是否平仓，平仓交给 `/close`

同一个 Telegram Token 只能由一个实例轮询。出现 `terminated by other getUpdates request` 时，关闭使用同一 Token 的其他机器人实例。

`/pnl` 会打印目标钱包与跟随钱包的完整 42 位地址，方便直接核对、复制到区块浏览器，而不是 `0x62f8…e7ac` 这类截断形式。实盘模式下，24H、7D、30D 使用账户净值历史计算；充值或提现也会影响净值变化。模拟模式没有跟随钱包的链上历史，因此这些周期显示为暂无数据。

每小时报告的第一行是跟单状态：

```text
🤖 跟单状态：🟢 正在跟单
🤖 跟单状态：⏸️ 暂停跟单，自 10-31 08:28 起（持仓保留，不复制新成交与挂单）
🤖 跟单状态：🔴 已停止（不再复制任何成交）
```

暂停后仍会每小时发报（用于确认进程还活着），所以状态行是区分“在跑”和“被我停了”的唯一依据。`🔴 已停止` 只在进程即将退出时出现，正常停机由单独的关机通知负责。

启动通知和 `/status` 末尾都会说明旧仓位的处理方式，文案来自同一处逻辑，只反映 `/copyloss` 的默认范围：

```text
• 追入旧仓位：手动：/copyloss 选择浮亏仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=loss
• 追入旧仓位：手动：/copyloss 选择浮盈仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=profit
• 追入旧仓位：手动：/copyloss 选择全部仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=all
```

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

### 新加的命令在 Telegram 里看不到

先发 `/start`，看最后一行有没有 `版本：`。**没有就是旧镜像在跑**：`/start` 的文本由进程内存里的代码生成，旧代码里既没有这一行，也没有 `/copyloss`，所以"命令没生效"和"没重启"看起来一模一样。

```bash
git pull
docker compose down
docker compose up -d --build     # 或直接 ./linux/start.sh
```

`docker compose restart` 只是重启容器，镜像里那份 `src/` 不会变，这也是最常踩的坑。重建后 `/start` 的版本行会显示新的 commit 短哈希；命令菜单还要等 Telegram 客户端刷新（重开聊天或等十几秒）。

### 只收到小时报告，没有跟单

先看报告第一行的 `🤖 跟单状态`：

- `⏸️ 暂停跟单`：被 `MAX_ACCOUNT_EQUITY` 自动暂停了。发 `/start` 恢复，但要先确认账户权益已回落，否则下一笔又会触发暂停。
- `🔴 已停止`：进程收到过 `/stop`。发 `/start` 恢复跟单，无需重启容器。

状态是 `🟢 正在跟单` 时，再检查目标是否真的有新的 `Open` 或 `Add` 成交；`Close` 或 `Reduce` 成交只有在跟随账户已有相同仓位时才会发送平仓单。用以下命令查看相关日志：

```bash
docker compose logs --since 24h copy-trader | grep -E 'FILL DETECTED|Fill copied|Failed to copy fill|Hyperliquid rejected'
```

如果日志里成交笔数正常但没有下单，注意 `$10` 最低名义额：目标的小额碎片化成交（例如 SKHX 常见的中位单笔 `$1,250`，按 `1%` 比例只有 `$12.5`）刚好在门槛附近，更小的会被跳过并单独推送 Telegram 通知。

### 日志出现 `Unknown asset symbol`

说明成交里的币种不在当前资产表中。程序会立刻刷新一次资产表重试；仍找不到时，该币种在 `ASSET_METADATA_TTL_SECONDS`（默认 300 秒）内不再触发刷新，避免一个不支持的标的把每笔成交都变成一次资产表拉取。

先确认拼写：builder DEX 标的是 `{dex}:{coin}`，dex 名区分行情来源，例如 `xyz:SKHX`（`perpDexs` 里的名字），而不是 `SKHX` 或 `SKHX-USD`。若确认拼写正确，可能是刚上架、索引尚未同步，等一个缓存周期再看。

### 余额显示为 0

`standard` 模式下，请将 USDC 从 Hyperliquid 的 Spot 账户转入 Perp 账户；该模式下每个 HIP-3 DEX 的保证金也是独立的，资金需要位于目标标的所属的 DEX 或默认 Perp 账户。

`unified` 模式下同一抵押币种的余额在现货与支持该币种的 Perp 市场间共用，但仍不能跨抵押币种互换。跟单时以 `activeAssetData` 返回的 `availableToTrade` 为准：`/positions` 里的余额为 0、而 `availableToTrade` 有额度属于正常情况，下单不会被拦。

## 本地运行

需要 Python 3.12：

```bash
pip install -r requirements.txt
pip install -r requirements-telegram.txt  # 仅在使用 Telegram 时需要
python src/main.py
```

## 免责声明

数字资产交易可能导致全部本金损失。软件按“现状”提供，不提供任何收益、可用性或适用性保证。使用者自行承担所有交易、配置和密钥管理风险。
