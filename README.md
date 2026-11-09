# Hyperliquid 跟单机器人

基于 Hyperliquid WebSocket 的跟单程序。它监听指定目标钱包的**新挂单和成交**，按跟随钱包与目标钱包的资金比例计算数量，并在 Hyperliquid 永续合约账户中提交对应订单。

> 风险提示：这是交易执行程序，不保证盈利。先使用模拟模式验证，再用少量资金开启实盘。私钥只应保存在本机或服务器的 `.env`，不要提交、截图或发送给任何人。

**目录**：[当前行为](#当前行为) · [快速开始](#快速开始) · [配置说明](#配置说明) · [Telegram](#telegram) · [常见问题](#常见问题) · [本地运行](#本地运行)

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
- **钱包零资金不等于进程挂了**：实盘启动时若跟随钱包净值为 `$0`，跟单停止但进程和 Telegram 都活着，只有 `/orders`、`/copyloss`、`/close` 被拒。入金后发 `/start` 自动恢复。
- **目标出入金会主动提醒**：每 `TARGET_EQUITY_FLOW_POLL_SECONDS` 扫一次目标的交易所流水，出现净出入金就推送 Telegram，并附对手方地址。跟单比例的分母就是这个净值，一次出入金会直接改变你后续每一笔的尺寸，详见[目标地址出入金提醒](#目标地址出入金提醒)。
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

`HYPERLIQUID_WALLET_ADDRESS` 必须填写由 `HYPERLIQUID_PRIVATE_KEY` 推导出的钱包地址（当前程序要求两者匹配）。`HYPERLIQUID_ACCOUNT_MODE` 只是兜底值：下单前程序会向交易所查 `userAbstraction`（返回 `unifiedAccount` / `portfolioMargin` / `default`）并以后者为准，所以账户里切换成统一账户后不需要改 `.env`，重启后日志会打出 `Follower Account Mode: unified (env: standard)` 这样的对照。两种模式的区别：

- `standard`（交易所返回 `default`）：现货、默认 Perp 和各 HIP-3 Perp DEX 的保证金分开。资金须位于该市场可用的 Perp 账户/DEX。缺失时按该 DEX 子账户的可用保证金算上限——一个 $151 的账户如果只往 `xyz` 划了 $33.57，就只能开 ~$134。
- `unified` / `portfolioMargin`：同一抵押币种的余额在现货与支持该币种的 Perp 市场间统一。程序改用 `activeAssetData` 读取当前市场、交易方向的可交易额度，不再看 DEX 子账户余额。不同抵押币种不会自动互换或合并。

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
python src/config/env_sync.py                 # 只看差异，不写文件
python src/config/env_sync.py --apply         # 修复 .env（写前备份到 data/env_backups/）
python src/config/env_sync.py --check         # 有差异时返回退出码 1，可用于脚本/巡检
python src/config/env_sync.py --example       # 由 schema 重新生成 .env.example
python src/config/env_sync.py --check-example # 校验 .env.example 与 schema 一致（构建门禁）
python src/config/env_sync.py --audit         # 源码读了但 schema 未声明的变量（构建门禁）
python src/config/env_sync.py --keep-removed  # 只补新增，不删失效项
python src/config/env_sync.py --no-backup     # 改动前不备份
```

只读文件系统或 `.env` 是单文件 bind mount 时，同步不会阻断启动：`rename(2)` 在挂载点上会返回 `EBUSY`，程序会退化为原地 truncate + fsync，保证文件能写进去、inode 不变。

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

判断线上到底跑的是哪一版：在 Telegram 发 `/start`，最后一行是 `版本：v1.1.0 +<commit>`。走 start 脚本构建时是 commit 短哈希；其它方式构建（没有 `BUILD_ID` 也没 `.git`）会显示 `版本：v1.1.0 +local@20261107-1215`，用镜像构建时间代替 commit，至少两个镜像之间能区分。**没有 `版本：` 这一行，就说明容器里还是旧镜像**——旧版本的帮助文本里也没有 `/close`，这正是"命令加了但看不到"的典型症状。

### 3. 切换到实盘前的检查

先在模拟模式观察新成交。看到以下日志，说明监听与尺寸计算正常：

```text
Fill copied: xyz:SKHX buy size=... target_size=... reduce_only=False
```

确认后将 `.env` 改为：

```properties
SIMULATED_TRADING=false
```

注意：实盘模式下如果 `HYPERLIQUID_WALLET_ADDRESS` 的账户净值为 0，程序**不退出**，而是直接把跟单停掉：进程继续跑、Telegram 照常在线，`/status` 显示 `💸 跟单已停止（钱包无资金，入金后发 /start）`，并且只禁用 `/orders`、`/copyloss`、`/close` 三个需要资金的命令，`/status`、`/pnl`、`/wallet`、`/follow`、`/leaderboard`、`/stop` 都不受影响。入金后发一次 `/start`，程序会重新读取净值、按到账金额重算跟单比例，然后自动恢复复制。以前这里直接抛异常，碰上 `restart: unless-stopped` 就变成每几秒重启一次的日志风暴，而且 Telegram 还没起来，看不到任何提示。

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

全部 43 个运行时配置项都在 [`.env.example`](.env.example) 里，带逐项注释，且由 `src/config/env_schema.py` 自动生成——**以它为准**，不要在这里背列表（这份 README 曾经因为手抄清单漏掉过新配置）。

必填的只有 4 项，其余都有合理默认值：

```properties
HYPERLIQUID_WALLET_ADDRESS=0x你的跟随钱包地址
HYPERLIQUID_PRIVATE_KEY=0x该钱包的私钥
HYPERLIQUID_ACCOUNT_MODE=standard   # 兜底值；实际以交易所 userAbstraction 为准
TARGET_WALLET_ADDRESS=0x要跟随的目标地址
```

改配置的正确流程：改 `.env` → 重启容器（`docker compose up -d`）。容器启动时会先跑一次 `.env` 对齐，新增项按代码默认值补齐、失效项移除，已有键不动。

最容易踩的几个开关：

| 配置 | 默认 | 说明 |
|---|---|---|
| `SIMULATED_TRADING` | `true` | 实盘必须显式改 `false`，没有其它逃生门 |
| `MAX_MARGIN_USAGE_RATIO` | `0.40` | **默认唯一生效的尺寸约束**，单笔最多用 40% 可交易额度 |
| `LEVERAGE_ADJUSTMENT` | `0.5` | 用目标杠杆的一半，再按交易所上限截断 |
| `MAX_POSITION_SIZE` | `x` | 单仓名义额上限；`x` = 不限制，填数字才生效 |
| `MAX_TOTAL_EXPOSURE` | `x` | 全部仓位合计名义额上限；`x` = 不限制 |
| `MAX_OPEN_TRADES` | `x` | 同时跟随的仓位数；`x` = 目标几个就跟几个 |
| `MAX_ACCOUNT_EQUITY` | `x` | 权益超过该值自动暂停；`x` = 不限制 |
| `COPY_PENDING_ORDERS` | `true` | 目标新挂单是否立即镜像 |
| `MIRROR_ORDER_PRICE` | `true` | 镜像用目标限价，还是跟随账户的中间价 |
| `TARGET_EQUITY_FLOW_MIN_PCT` | `10000` | 净值残差兜底的比例阈值；10000 = 关闭兜底，只报流水确认的出入金 |
| `BLOCKED_ASSETS` | 空 | 不跟随的币种，逗号分隔，如 `BTC,ETH,SOL` |

`BUILD_ID`、`LIVE_ORDER_TEST_CONFIRM` 不是业务配置：前者由 start 脚本在构建时注入（用于 `/start` 显示 commit），后者是验证脚本的一次性确认词。

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

- `/start` 启动或恢复跟单：暂停后用它恢复；停止后也用它恢复，无需重启容器（恢复时重新连接目标监控，不会补跟停止期间的历史成交）。最后一行是运行版本
- `/status` 查看运行状态、跟单配置和**自己的持仓**（`/positions` 是这个命令的别名）
- `/orders` 查看挂单
- `/pnl` 查看收益摘要，含目标钱包与跟随钱包的完整地址
- `/leaderboard` 查看 Hyperliquid 公开排行榜，可按 24H、7D、30D 的收益额或收益率筛选；最多读取 200 名，每页 10 名
- `/wallet 0x地址 [1-20]` 查询任意公开地址的账户状态、24H/7D/30D 净值变化和最近成交；默认显示 10 笔
- `/follow 0x地址` 更换跟单目标，同时重建资金监控基线
- `/copyloss [loss|all|profit] [序号...] [confirm] [force]` 手动复制目标现有仓位（启动时永不自动复制）：不带参数先列出编号清单、每笔按比例应买的金额，以及扣掉保证金上限后**实际会下的金额**（两者不一致时说明是哪个 DEX 的可用保证金不够）；`/copyloss 1 3` 只选第 1、3 笔；加 `confirm` 才真正下单，随后返回逐笔回执（成交价、数量、名义额、订单号，失败项带原因；名义额按真实成交价重算）。已持有、持有反方向、或 30 分钟内复制过的会在预览里标出并在下单时拦下；确需叠加同一仓位追加 `force`。
- `/close [序号|币种|all] [confirm]` 平掉**跟随账户自己**的仓位：不带参数列出编号清单（数量、≈金额、未实现盈亏）；可用序号（`/close 1 3 confirm`）、币种片段（`/close BTC confirm`、`/close xyz:SKHX confirm`）或 `all` 选择，加 `confirm` 才发送 reduce-only 市价单，随后返回逐笔回执（平仓方向、成交价、名义额、已实现盈亏、订单号，失败项带原因）。暂停或停止状态下都能用
- `/stop` 停止跟单：取消全部挂单、停止复制，**保留所有仓位**；不再询问是否平仓，平仓交给 `/close`

命令面只有这 10 项（`/positions` 为别名，不进菜单）。`/pause`、`/resume` 这类旧命令已经从代码和 Telegram 菜单里移除：菜单在默认范围和授权的 chat 范围都会重写，同时清掉 `all_private_chats`、`all_group_chats`、`all_chat_administrators` 三个历史 scope，否则旧列表会一直挂在客户端里。

实盘启动时跟随钱包净值为 `$0`，跟单会自动停止，只有 `/orders`、`/copyloss`、`/close` 三个需要资金的命令被拒绝（其余命令照常可用）。入金后发 `/start` 会重新读取净值、重算跟单比例并恢复复制。此时 `/status` 长这样：

```text
📊 跟单运行状态
💸 状态：跟单已停止（钱包无资金，入金后发 /start）
🎮 模式：实盘
👤 目标：0x62f87be0...75e7ac
💼 账户余额：$0.00
📈 未实现盈亏：$0.00
📊 已复制成交：0
📍 持仓数：0
自己的持仓
暂无持仓
⏰ 运行时长：1.8 小时
仓位模式：Proportional
杠杆：目标杠杆的 1.0 倍
追入旧仓位：手动：/copyloss 选择浮亏仓位（启动不自动追入）
资金限制：钱包净值 $0，/orders /copyloss /close 已禁用；入金后发 /start
版本：v1.1.0 +1fa371b
```

`资金限制` 这行只在零资金状态下出现，正常情况下整行不显示。

同一个 Telegram Token 只能由一个实例轮询。出现 `terminated by other getUpdates request` 时，关闭使用同一 Token 的其他机器人实例。

`/pnl` 会打印目标钱包与跟随钱包的完整 42 位地址，方便直接核对、复制到区块浏览器，而不是 `0x62f8…e7ac` 这类截断形式。实盘模式下，24H、7D、30D 使用账户净值历史计算；充值或提现也会影响净值变化。模拟模式没有跟随钱包的链上历史，因此这些周期显示为暂无数据。

每小时报告的第一行是跟单状态：

```text
🤖 跟单状态：🟢 正在跟单
🤖 跟单状态：⏸️ 暂停跟单，自 10-31 08:28 起（持仓保留，不复制新成交与挂单）
🤖 跟单状态：🔴 已停止（不再复制任何成交）
```

暂停后仍会每小时发报（用于确认进程还活着），所以状态行是区分“在跑”和“被我停了”的唯一依据。`🔴 已停止` 表示收到过 `/stop`，此时跟单已停但进程仍在（Telegram 可用），发 `/start` 即可恢复，不需要重启容器；进程真正退出时另有单独的关机通知。

启动通知和 `/status` 末尾都会说明旧仓位的处理方式，文案来自同一处逻辑，只反映 `/copyloss` 的默认范围：

```text
• 追入旧仓位：手动：/copyloss 选择浮亏仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=loss
• 追入旧仓位：手动：/copyloss 选择浮盈仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=profit
• 追入旧仓位：手动：/copyloss 选择全部仓位（启动不自动追入）   COPY_OPEN_PNL_FILTER=all
```

### 目标地址出入金提醒

按账户净值比例跟单时，目标钱包一次入金或出金会直接放大／缩小你后续每一笔单，而交易所不会推送「有人充值了」这种事件。机器人每 `TARGET_EQUITY_FLOW_POLL_SECONDS`（默认 60 秒）扫一次，分两条路径判断，命中任一就发 Telegram 通知。

**主路径：交易所流水（权威，覆盖现货）**

每轮固定读 `userNonFundingLedgerUpdates`，窗口回溯到上次成功扫描的位置再往前 90 秒（吸收索引延迟），最远回溯 24 小时。净额 = 充值 − 提现 + 有方向的地址间划转：

- **不受金额/比例阈值限制**，真正的现金进出不是噪声，只有低于 `$1` 的尘埃级流水会被丢弃；
- **自己搬钱不算**。目标把 USDC 从现货划到合约（或反向）时，流水里也表现为 `send`，但收发双方都是本钱包，直接排除，不当作入金也不当作出金；
- **只统计 USDC 现金**。`spotTransfer` 里的 `usdcValue` 是对代币的估值，不是现金流，计入会虚增；只认 `usdc` 字段和 USDC 代币的 `amount`；
- **同一个窗口不重复报**。窗口故意重叠以吸收索引延迟，靠事件的 nonce（`event_key`）去重；流水与净值两条路径也会互相登记金额，同一笔不会因为"流水先到、净值后跳"而推两条。重复推送比漏报更烦人。
- **两条路径共用冷却窗口**：`TARGET_EQUITY_FLOW_COOLDOWN_SECONDS`（默认 300 秒）内最多推一条，避免分批转账刷屏。

**兜底路径：净值残差（默认关闭）**

只有流水还没索引到、但净值已经跳了的时候才用得上：

```text
推断净流转 = (本次总净值 − 上次总净值) − (本次未实现盈亏 − 上次未实现盈亏)
```

同时满足三条才告警：仓位变化 ≤ `TARGET_EQUITY_FLOW_MAX_POSITION_CHANGE_PCT`（默认 5%，排除"目标交易了"）、残差 ≥ `TARGET_EQUITY_FLOW_MIN_USD`（默认 $100）且 ≥ `TARGET_EQUITY_FLOW_MIN_PCT`（占总净值百分比）、不在冷却窗口内。

净值基准是**全账户总金额**（`portfolio` 口径，perp + 现货），和账户盈亏摘要里那行同源——目标把资金全放现货时，只看合约账户净值会读到 `$0`，监控直接失明。代价是这个口径含现货代币的价格波动，所以 `TARGET_EQUITY_FLOW_MIN_PCT` 默认是 **10000**，等于把兜底路径关掉：只报流水确认过的事实，不猜。需要兜底时把它改成 `1.0` 之类的小值。

通知里的金额和对手方都直接给出，不用去区块浏览器反查：

```text
📤 目标地址资金异动 · 转出 / 出金

👤 目标：0x62f87be0...75e7ac
💵 交易所流水净额：$130,000.00（-21.57%）
全账户净值：$602,795.90（portfolio 口径，含现货）

━━━━━━━━━━━━━━━━━━
🔗 流水明细：外部转出 $130,000.00（系统地址）；流水类型：send × 1
```

同一窗口里既有进又有出时，两个方向分别列出（`$110,215.30（0x6b9e…0a24）` / `$130,000.00（系统地址）`），不会被净额吃掉。`0x2000…0000` 是 Hyperliquid 的现货 USDC 系统地址，打到它的钱**是真的从账户净值里出去了**（已用净值曲线逐区间对账验证，误差只有手续费），所以标记为「系统地址」但仍然算转出。

几个边界：

- 首次扫描只建立游标/基线，不告警；`/follow` 切换目标后自动重建。
- 流水查询失败不推进游标，下一轮重新覆盖同一窗口；净值读取失败或读到 0 时不更新基线——部分 DEX 聚合失败看起来就像一笔巨额出金。
- 进程重启会丢掉游标和去重集合，最多回溯 24 小时；停机更久期间的流水不补报。
- 监控是只读的，`/stop` 之后仍然运行。`/status` 不显示监控行，避免把"上次采样时间"当成跟单状态。

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

先发 `/start`，看最后一行有没有 `版本：`。**没有就是旧镜像在跑**：`/start` 的文本由进程内存里的代码生成，旧代码里既没有这一行，也没有 `/close`，所以"命令没生效"和"没重启"看起来一模一样。

```bash
git pull
docker compose down
docker compose up -d --build     # 或直接 ./linux/start.sh
```

`docker compose restart` 只是重启容器，镜像里那份 `src/` 不会变，这也是最常踩的坑。重建后 `/start` 的版本行会显示新的 commit 短哈希；命令菜单还要等 Telegram 客户端刷新（重开聊天或等十几秒）。

菜单里有、发出去却报「未知命令」，是另一个问题：旧版本把命令注册在 chat scope，和 BotFather 的默认 scope 是两份独立数据。现在的代码启动时会把默认范围、授权 chat 范围以及 `all_private_chats` / `all_group_chats` / `all_chat_administrators` 全部重写一遍，并在日志里回读校验；如果菜单仍不对，先看日志有没有 `Failed to publish Telegram command menu`。

### 只收到小时报告，没有跟单

先看报告第一行的 `🤖 跟单状态`：

- `⏸️ 暂停跟单`：被 `MAX_ACCOUNT_EQUITY` 自动暂停了。发 `/start` 恢复，但要先确认账户权益已回落，否则下一笔又会触发暂停。
- `💸 跟单已停止（钱包无资金）`：实盘启动时跟随钱包净值为 `$0`。入金后发 `/start`。
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

`unified` 模式下同一抵押币种的余额在现货与支持该币种的 Perp 市场间共用，但仍不能跨抵押币种互换。跟单时以 `activeAssetData` 返回的 `availableToTrade` 为准：`/positions` 里的余额为 0、而 `availableToTrade` 有额度属于正常情况，下单不会被拦。同理，交易所给某个 HIP-3 DEX 子账户算出来的 `withdrawable`／可用保证金会远小于账户真实可用资金（实测过 $4.44 对 $123.26），那只是该 DEX 已占用保证金后的残值，不是下单上限。

## 本地运行

需要 Python 3.12：

```bash
pip install -r requirements.txt
pip install -r requirements-telegram.txt  # 仅在使用 Telegram 时需要
python src/main.py
```

## 免责声明

数字资产交易可能导致全部本金损失。软件按“现状”提供，不提供任何收益、可用性或适用性保证。使用者自行承担所有交易、配置和密钥管理风险。
