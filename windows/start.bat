@echo off
echo Starting Hyperliquid Copy Trader with Docker...
rem .env must be a real file: compose mounts ./.env into the container, and if
rem it is missing docker creates a directory instead, so every setting silently
rem falls back to its default.
if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo No .env found - copied .env.example. Fill in the private key and target address.
)
rem Align .env with the code: append newly added options with their code
rem defaults, drop options that no longer exist (backup in data\env_backups).
rem Existing values are never modified. Skipped if python is not installed.
where python >nul 2>nul
if errorlevel 1 (
  echo python not found - skipping .env sync, the container will do it on start.
) else (
  python "src\config\env_sync.py" --apply
)
rem --build is required: the image carries a snapshot of src\ taken at build
rem time, so "up -d" alone keeps running the previous code.
docker-compose up -d --build
echo.
echo Bot started! Use 'docker-compose logs -f' to view logs
echo Check the live build with /start in Telegram (version line).
echo Use 'docker-compose down' to stop the bot
pause
