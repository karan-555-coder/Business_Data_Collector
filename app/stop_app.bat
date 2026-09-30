@echo off
rem Stops the Business Data Collector (visible or hidden instance).
rem Progress is always saved - collections resume on next start.
rem The web process is stopped first; its collection worker then stops every
rem running collection, writes the final checkpoint and exits by itself -
rem this script waits for that (up to 40 s) so a quick restart can never
rem race the final save.
echo Stopping Business Data Collector...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /C:":8100 " ^| findstr /C:"LISTENING"') do (
  taskkill /PID %%p /F >nul 2>&1
  echo Saving collection progress...
  powershell -NoProfile -Command "$t=(Get-Date).AddSeconds(40); while ((Get-Date) -lt $t -and (Get-CimInstance Win32_Process -Filter \"CommandLine like '%%parent_pid=%%p,%%'\")) { Start-Sleep -Milliseconds 500 }"
)
echo Stopped. Start it again with the Desktop shortcut or start_app.bat.
ping -n 2 127.0.0.1 >nul
