@echo off
setlocal

set "APP_NAME=Enhanced Living Whisperwood Demo"
set "APP_EXE=WhisperwoodDemo.exe"
set "TARGET=%LOCALAPPDATA%\Programs\%APP_NAME%\%APP_EXE%"

rem Give the older updater launcher time to reopen the previous Program Files copy,
rem then close only that stale copy and start the new per-user app.
timeout /t 7 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$target = Join-Path $env:LOCALAPPDATA 'Programs\Enhanced Living Whisperwood Demo\WhisperwoodDemo.exe'; Get-Process WhisperwoodDemo -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path -ne $target) } | Stop-Process -Force -ErrorAction SilentlyContinue; if (Test-Path $target) { Start-Process $target }"

endlocal
