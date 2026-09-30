@echo off
rem Network launcher: like start_app.bat, but also reachable from phones and
rem tablets on the SAME Wi-Fi / LAN. Anyone on your network can then open the
rem app and start collections, so use the normal start_app.bat when you don't
rem need that. If Windows Firewall asks, click "Allow access".
cd /d "%~dp0"

netstat -ano | findstr /C:":8100 " | findstr /C:"LISTENING" >nul 2>&1
if %errorlevel%==0 (
  echo The app is already running. Close its window first, then run this again.
  pause
  exit /b 1
)

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo Open the app on this PC:   http://127.0.0.1:8100
echo Open it on phone / tablet (same Wi-Fi) at one of these addresses:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do echo    http://%%a:8100
echo.

start "" /min cmd /c "timeout /t 2 >nul & start http://127.0.0.1:8100"
echo (Keep this window open. Press Ctrl+C to stop; progress is saved.)
"%PY%" -m backend.serve --host 0.0.0.0 --port 8100
pause
