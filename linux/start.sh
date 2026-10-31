#!/bin/bash
# Restart the bot with the code that is currently on disk.
#
# `docker-compose up -d` alone reuses the existing image, and the image holds a
# copy of src/ taken at build time, so a plain restart silently keeps running
# the old code -- which looks exactly like "my new command was never added".
set -e
cd "$(dirname "$0")/.."

export BUILD_ID="$(git rev-parse --short HEAD 2>/dev/null || echo local)"

# .env 必须是一个真实文件：compose 把 ./.env 挂进容器，若它不存在，docker 会
# 建一个同名目录，之后 load_dotenv 读到目录、配置全部落回默认值。
if [ ! -f .env ]; then
  cp .env.example .env
  echo "未找到 .env，已从 .env.example 复制一份，请填入私钥与目标地址。"
fi

# 把 .env 与代码对齐：新增配置补上（用代码默认值，不改变现有行为），
# 已经从代码移除的配置删掉（先备份到 data/env_backups/），已有值一律不动。
# 没有本机 python3 也不影响，容器启动时还会再同步一次。
if command -v python3 >/dev/null 2>&1; then
  python3 src/config/env_sync.py --apply || echo "（.env 同步未完成，容器启动时会重试）"
else
  echo "未检测到 python3，跳过 .env 同步（容器启动时会自动完成）。"
fi

echo "Starting Hyperliquid Copy Trader with Docker (build $BUILD_ID)..."
docker-compose up -d --build
echo ""
echo "Bot started! Use 'docker-compose logs -f' to view logs"
echo "Check the live build with /start in Telegram (版本 line)."
echo "Use 'docker-compose down' to stop the bot"
