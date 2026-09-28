@echo off
rem Token 领取助手 - 双击启动（pythonw 无黑窗口）
cd /d "%~dp0"
if exist "%LOCALAPPDATA%\Python\bin\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Python\bin\pythonw.exe" token_claimer.py
    exit /b
)
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw token_claimer.py
    exit /b
)
start "" python token_claimer.py
