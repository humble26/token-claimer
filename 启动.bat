@echo off
chcp 65001 >nul
rem Token Claimer launcher - double click to start (pythonw, no console window).
rem
rem It picks the FIRST interpreter that can actually `import tkinter`. A Python
rem without tcl/tk dies at import time, and pythonw swallows the error - the user
rem just sees a window flash and vanish. If no candidate works, this window stays
rem open with a readable message instead of disappearing.
rem
rem It no longer blindly prefers "%LOCALAPPDATA%\Python\bin\pythonw.exe": that
rem path can exist while pointing at an interpreter without tkinter.
cd /d "%~dp0"

set "PYW="
call :probe "%LOCALAPPDATA%\Python\bin\pythonw.exe"
if defined PYW goto launch
call :probe "pythonw"
if defined PYW goto launch
call :probe "pyw"
if defined PYW goto launch

echo.
echo [ERROR] 没有找到带 tkinter 的 Python，程序无法启动。
echo         请安装 Python 3.8+，安装时勾选 "tcl/tk and IDLE"。
echo         下载地址： https://www.python.org/downloads/
echo.
pause
exit /b 1

:probe
rem %1 = pythonw 可执行文件（绝对路径或 PATH 里的名字）
rem 路径不存在、没有 tkinter 都会返回非 0，直接跳过。
"%~1" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto :eof
set "PYW=%~1"
goto :eof

:launch
start "" "%PYW%" token_claimer.py
