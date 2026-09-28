@echo off
rem 打包为独立 EXE（无需安装 Python 的电脑也能运行）
cd /d "%~dp0"
echo 正在安装/更新 PyInstaller 与 comtypes（首次需联网，约 1 分钟）...
python -m pip install --quiet --disable-pip-version-check --upgrade comtypes pyinstaller
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Token领取助手" token_claimer.py
if exist "dist\Token领取助手.exe" (
    echo.
    echo 打包完成: %~dp0dist\Token领取助手.exe
    echo 可将该 EXE 单独复制到任意位置使用，配置保存在其同目录下。
) else (
    echo.
    echo 打包失败，请检查上方错误信息。
)
pause
