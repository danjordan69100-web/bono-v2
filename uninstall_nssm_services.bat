@echo off
REM Bono v2 — Uninstall Windows services (NSSM)
REM Run as Administrator

set NSSM=nssm.exe
where %NSSM% >nul 2>&1
if errorlevel 1 (
    if exist "C:\dev\bono_v2\nssm.exe" set NSSM=C:\dev\bono_v2\nssm.exe
)

echo === Bono v2 NSSM service uninstall ===
for %%S in (Core Playback Input Mcp Telegram) do (
    echo Stopping Bono%%S...
    net stop Bono%%S 2>nul
    echo Removing Bono%%S...
    %NSSM% remove Bono%%S confirm
)
echo Done.
pause
