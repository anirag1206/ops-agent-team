@echo off
cd /d "%~dp0"
:loop
python run.py middleman
echo Middleman stopped, restarting in 10 seconds...
timeout /t 10 >nul
goto loop
