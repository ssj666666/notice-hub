@echo off
rem ============================================================
rem  notice-hub desktop app launcher
rem  Opens a native window (pywebview) or a chromeless Edge app
rem  window if pywebview is unavailable.
rem
rem  First run creates .venv and installs dependencies.
rem  Kept ASCII-only on purpose: cmd.exe mangles UTF-8 text.
rem ============================================================
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto deps

echo [notice-hub] First run: creating virtual environment...
py -3.12 -m venv .venv
if exist ".venv\Scripts\python.exe" goto deps
py -3 -m venv .venv
if exist ".venv\Scripts\python.exe" goto deps
python -m venv .venv

:deps
if not exist ".venv\Scripts\python.exe" goto noenv
if exist ".venv\.deps-ok" goto run
echo [notice-hub] Installing dependencies (may take a minute)...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto piperr
echo ok> ".venv\.deps-ok"

:run
".venv\Scripts\python.exe" app_window.py
if errorlevel 1 pause
goto end

:noenv
echo.
echo [ERROR] Could not create virtual environment.
echo         Install Python 3.10+ (python.org) and make sure "py" works.
echo.
pause
goto end

:piperr
echo.
echo [ERROR] pip install failed. Check network or proxy settings.
echo.
pause
goto end

:end
endlocal
