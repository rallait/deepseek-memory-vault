@echo off
chcp 65001 >nul
REM ============================================================
REM  DeepShare - DeepSeek Share Memory Vault  Startup (Windows)
REM  First run auto-installs deps + Playwright chromium.
REM ============================================================

cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo [X] python not found. Please install Python 3.10+ and add to PATH.
    pause
    exit /b 1
)

echo [*] Python:
python --version
echo.

if not exist ".deps_ok" (
    echo [*] First run - installing dependencies...
    python -m pip install -r requirements.txt || goto :err
    python -m playwright install chromium || goto :err
    echo ok > .deps_ok
    echo [OK] Dependencies ready.
    echo.
)

echo [*] Starting Streamlit...
python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
goto :eof

:err
echo [X] Error - see messages above.
pause
exit /b 1