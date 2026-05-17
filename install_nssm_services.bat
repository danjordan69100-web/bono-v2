@echo off
REM Bono v2 — Install Windows services via NSSM
REM Run as Administrator
REM
REM Pre-req : download NSSM from https://nssm.cc/download (extract nssm.exe to PATH or this dir)
REM
REM What it does :
REM   - Creates 5 Windows services (BonoCore, BonoPlayback, BonoInput, BonoMcp, BonoTelegram)
REM   - Configures exponential restart backoff (1s -> 60s)
REM   - Sets working dir to C:\dev\bono_v2
REM   - Logs to C:\dev\bono_v2\logs\<service>.{out,err}.log
REM
REM Uninstall : uninstall_nssm_services.bat

setlocal enabledelayedexpansion

set BONO_DIR=C:\dev\bono_v2
set PY_EXE=C:\Users\danjo\AppData\Local\Programs\Python\Python311\python.exe
set NSSM=nssm.exe

REM Locate nssm.exe
where %NSSM% >nul 2>&1
if errorlevel 1 (
    if exist "%BONO_DIR%\nssm.exe" (
        set NSSM=%BONO_DIR%\nssm.exe
    ) else (
        echo [ERROR] nssm.exe not found in PATH or %BONO_DIR%
        echo Download from https://nssm.cc/download and put nssm.exe in %BONO_DIR%
        pause
        exit /b 1
    )
)

echo === Bono v2 NSSM service install ===
echo Bono dir : %BONO_DIR%
echo Python   : %PY_EXE%
echo NSSM     : %NSSM%
echo.

if not exist "%PY_EXE%" (
    echo [ERROR] Python not found at %PY_EXE%
    pause
    exit /b 1
)

REM Verify all scripts exist (explicit mapping name=script_filename)
call :verify_script BonoCore core_service.py
call :verify_script BonoPlayback playback_service.py
call :verify_script BonoInput input_service.py
call :verify_script BonoMcp mcp_server.py
call :verify_script BonoTelegram telegram_bot.py

REM Install each service explicitly (no fragile suffix-guessing)
call :install_svc BonoCore core_service.py
call :install_svc BonoPlayback playback_service.py
call :install_svc BonoInput input_service.py
call :install_svc BonoMcp mcp_server.py
call :install_svc BonoTelegram telegram_bot.py

echo.
echo === Done ===
echo To start all services (run in any order, NSSM handles dependencies via restart):
echo   net start BonoCore
echo   net start BonoPlayback
echo   net start BonoInput
echo   net start BonoMcp
echo   net start BonoTelegram
echo.
echo Check status: sc query BonoCore   ^|^|   %NSSM% status BonoCore
echo Uninstall   : uninstall_nssm_services.bat
echo.
pause
exit /b 0


:verify_script
REM args: %1 = svc name, %2 = script filename
if not exist "%BONO_DIR%\%~2" (
    echo [ERROR] Missing script for %~1 : %BONO_DIR%\%~2
    pause
    exit /b 1
)
exit /b 0


:install_svc
REM args: %1 = svc name, %2 = script filename
set "SVC=%~1"
set "SCRIPT=%~2"
echo --- Installing service: %SVC% (%SCRIPT%) ---
REM Remove if exists (idempotent install)
%NSSM% status "%SVC%" >nul 2>&1
if not errorlevel 1 (
    echo   service exists, stopping + removing first
    net stop "%SVC%" >nul 2>&1
    %NSSM% remove "%SVC%" confirm >nul 2>&1
)
%NSSM% install "%SVC%" "%PY_EXE%" "%BONO_DIR%\%SCRIPT%"
%NSSM% set "%SVC%" AppDirectory "%BONO_DIR%"
%NSSM% set "%SVC%" AppStdout "%BONO_DIR%\logs\%SVC%.nssm.out.log"
%NSSM% set "%SVC%" AppStderr "%BONO_DIR%\logs\%SVC%.nssm.err.log"
%NSSM% set "%SVC%" AppRotateFiles 1
%NSSM% set "%SVC%" AppRotateOnline 1
%NSSM% set "%SVC%" AppRotateBytes 10485760
%NSSM% set "%SVC%" AppExit Default Restart
%NSSM% set "%SVC%" AppRestartDelay 1000
%NSSM% set "%SVC%" AppThrottle 5000
%NSSM% set "%SVC%" Start SERVICE_AUTO_START
%NSSM% set "%SVC%" AppEnvironmentExtra "PYTHONIOENCODING=utf-8" "BONO_TTS_PROVIDER=elevenlabs"
echo   [OK] %SVC% installed
exit /b 0
