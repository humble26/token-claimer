@echo off
chcp 65001 >nul
rem 打包为独立 EXE（无需安装 Python 的电脑也能运行）
rem
rem 除 comtypes 外还要装 numpy + opencv-python：自动过滑块（验证码）依赖它们
rem 做图像定位。不装也能打包成功，只是运行时 captcha_solver.available() 为假、
rem 自动过滑块自动停用并退回人工 —— 想让功能可用就别删这两个包。
rem playwright 只有求解库的浏览器通道才用得到，本工具走屏幕截图路线，显式排除
rem 以免 PyInstaller 因缺包报警并把体积撑大。
cd /d "%~dp0"
echo 正在安装/更新 PyInstaller、comtypes、numpy、opencv（首次需联网，约 1-3 分钟）...
python -m pip install --quiet --disable-pip-version-check --upgrade comtypes pyinstaller numpy opencv-python
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "Token领取助手" ^
    --exclude-module playwright ^
    token_claimer.py
if exist "dist\Token领取助手.exe" (
    echo.
    echo 打包完成: %~dp0dist\Token领取助手.exe
    echo 可将该 EXE 单独复制到任意位置使用，配置保存在其同目录下。
) else (
    echo.
    echo 打包失败，请检查上方错误信息。
)
pause
