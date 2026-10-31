@echo off
echo Starting Hyperliquid Copy Trader with Docker...
rem --build is required: the image carries a snapshot of src\ taken at build
rem time, so "up -d" alone keeps running the previous code.
docker-compose up -d --build
echo.
echo Bot started! Use 'docker-compose logs -f' to view logs
echo Check the live build with /start in Telegram (version line).
echo Use 'docker-compose down' to stop the bot
pause
