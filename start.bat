@echo off
chcp 65001 >nul
REM ============================================================
REM  DeepShare · DeepSeek 分享抓取记忆库  启动脚本（Windows）
REM  第一次跑会自动装依赖 + 拉 Playwright 浏览器，之后秒开。
REM ============================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [X] 没找到 python，请先装 Python 3.10+ 并加 PATH。
    pause
    exit /b 1
)

echo [*] Python 版本:
python --version
echo.

REM --- 依赖 ---
if not exist ".deps_ok" (
    echo [*] 第一次启动，正在装依赖...
    python -m pip install -r requirements.txt || goto :err
    python -m playwright install chromium || goto :err
    echo ok > .deps_ok
    echo [√] 依赖安装完成。
    echo.
)

echo [*] 启动 Streamlit...
python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
goto :eof

:err
echo [X] 出错了，请看上面的报错信息。
pause
exit /b 1
