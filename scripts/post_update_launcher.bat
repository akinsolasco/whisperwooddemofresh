@echo off
setlocal

set "APP_NAME=Enhanced Living Whisperwood Demo"
set "APP_EXE=WhisperwoodDemo.exe"
set "TARGET=%LOCALAPPDATA%\Programs\%APP_NAME%\%APP_EXE%"

rem Let the installer release files, then start exactly one per-user app copy.
timeout /t 2 /nobreak >nul

powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "$target = Join-Path $env:LOCALAPPDATA 'Programs\Enhanced Living Whisperwood Demo\WhisperwoodDemo.exe'; Get-Process WhisperwoodDemo -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path -ne $target) } | Stop-Process -Force -ErrorAction SilentlyContinue; $running = Get-Process WhisperwoodDemo -ErrorAction SilentlyContinue | Where-Object { $_.Path -and ($_.Path -eq $target) } | Select-Object -First 1; if ((-not $running) -and (Test-Path $target)) { Start-Process $target }"

endlocal
