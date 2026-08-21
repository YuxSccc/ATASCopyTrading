@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bridge.ps1" %*
if errorlevel 1 (
    echo.
    echo Bridge failed to start. Press any key to close this window.
    pause >nul
)
endlocal
