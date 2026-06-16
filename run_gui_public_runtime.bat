@echo off
setlocal
cd /d "%~dp0"
set "TWITCH_STREAM_BOT_RUNTIME_MODE=installed"
echo Starting Twitch Stream Bot with the same settings/data folder as the public app...
echo Runtime data: %LOCALAPPDATA%\Twitch Song Request Bot
python run_gui.py
pause
