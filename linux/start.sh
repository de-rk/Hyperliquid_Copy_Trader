#!/bin/bash
# Restart the bot with the code that is currently on disk.
#
# `docker-compose up -d` alone reuses the existing image, and the image holds a
# copy of src/ taken at build time, so a plain restart silently keeps running
# the old code -- which looks exactly like "my new command was never added".
set -e
cd "$(dirname "$0")/.."

export BUILD_ID="$(git rev-parse --short HEAD 2>/dev/null || echo local)"

echo "Starting Hyperliquid Copy Trader with Docker (build $BUILD_ID)..."
docker-compose up -d --build
echo ""
echo "Bot started! Use 'docker-compose logs -f' to view logs"
echo "Check the live build with /start in Telegram (版本 line)."
echo "Use 'docker-compose down' to stop the bot"
