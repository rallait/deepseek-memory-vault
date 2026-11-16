@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
REM ============================================================
REM  DeepShare - DeepSeek Share Memory Vault  Startup (Windows)
REM  First run auto-installs deps + Playwright chromium.
REM  No interactive prompts ever - browser auto-opens.
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

REM --- Kill any previous instance on port 8501 ---
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr :8501 ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM --- Start Streamlit in background (headless = skips email wizard) ---
echo [*] Starting Streamlit on http://localhost:8501 ...
start /B "" python -m streamlit run app.py --server.headless true --server.address localhost --browser.gatherUsageStats false

REM --- Wait for server ---
echo [*] Waiting for server to be ready...
timeout /t 4 /nobreak >nul

REM --- Open browser ---
echo [*] Opening browser...
start "" "http://localhost:8501"
echo [OK] Done. This window can be closed anytime - Streamlit keeps running.
echo     To stop: run "taskkill /F /IM python.exe" or restart your computer.
goto :eof

:err
echo [X] Error - see messages above.
pause
exit /b 1