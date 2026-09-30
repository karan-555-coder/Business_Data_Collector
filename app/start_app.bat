@echo off
rem One-click launcher for the Business Data Collector demo.
rem Starts the backend on http://127.0.0.1:8100 (if not already running)
rem and opens the browser. Safe to double-click any time.
cd /d "%~dp0"

rem Already running? Just open the browser.
netstat -ano | findstr /C:":8100 " | findstr /C:"LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo Business Data Collector is already running - opening the browser.
  start "" http://127.0.0.1:8100
  timeout /t 3 >nul
  exit /b 0
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

rem Open the browser shortly after; the page retries until the API is up.
start "" /min cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8100"

echo Starting Business Data Collector on http://127.0.0.1:8100 ...
echo (Keep this window open. Press Ctrl+C to stop; progress is saved.)
"%PY%" -m backend.serve --host 127.0.0.1 --port 8100
pause
