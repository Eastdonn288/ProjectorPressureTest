@echo off
chcp 65001 >nul 2>&1

set "ROOT_DIR=%~dp0"
cd /d "%ROOT_DIR%"

if not exist "%ROOT_DIR%logs" mkdir "%ROOT_DIR%logs"
if not exist "%ROOT_DIR%data" mkdir "%ROOT_DIR%data"

echo.
echo ============================================
echo   PPTP - Projector Pressure Test Platform
echo   v2.0
echo ============================================
echo.

REM ---- Pre-check: is port 8000 already serving PPTP? ----
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   [INFO] Port 8000 is already listening.
    echo   PPTP may already be running - open http://127.0.0.1:8000 directly.
    echo   This window will close in 2 seconds.
    timeout /t 2 /nobreak >nul
    exit /b 0
)

REM ---- Find Python: PATH first, then common conda/env paths ----
set "PYTHON="
where python >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%p in ('where python') do (
        set "PYTHON=%%p"
        goto :py_found
    )
)

set "TRY_PATHS=D:\Conda_Environments\dev_env\python.exe D:\anaconda3\python.exe D:\Miniconda3\python.exe C:\Anaconda3\python.exe C:\ProgramData\Anaconda3\python.exe %USERPROFILE%\anaconda3\python.exe %USERPROFILE%\miniconda3\python.exe C:\Python311\python.exe C:\Python310\python.exe %LOCALAPPDATA%\Programs\Python\Python311\python.exe %LOCALAPPDATA%\Programs\Python\Python310\python.exe"
for %%p in (%TRY_PATHS%) do (
    if exist "%%p" if "%PYTHON%"=="" set "PYTHON=%%p"
)

if "%PYTHON%"=="" goto :no_python
:py_found
for %%p in ("%PYTHON%") do echo   [OK] Python: %%~nxp at %%~p

REM ---- Check adb (optional) ----
where adb >nul 2>&1
if errorlevel 1 (
    echo   [WARN] adb not in PATH - device features unavailable
) else (
    for /f "tokens=*" %%v in ('adb --version 2^>nul') do echo   [OK] ADB: %%v
)

echo.
echo   Starting uvicorn on http://127.0.0.1:8000 ...
echo   Live server logs show in the PPTP-Server window
echo   (also saved to logs\server.out.log and logs\server.err.log)
echo.

REM ---- Start server in its own visible window (see server_window.ps1) ----
REM Closing the PPTP-Server window stops the server.
start "PPTP-Server" /D "%ROOT_DIR%" powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%server_window.ps1" "%PYTHON%"

REM ---- Wait for port ----
set "i=0"
:wait_port
if %i% geq 20 goto :wait_timeout
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto :server_ok
set /a i+=1
goto :wait_port

:wait_timeout
echo.
echo   [FAIL] Server did not listen on port 8000 within 20 seconds.
echo   Please check:
echo     1. The error shown in the PPTP-Server window
echo        (missing dependency / wrong Python version / bad path)
echo     2. logs\server.err.log
echo     3. Whether port 8000 is occupied by another program
echo   ----------------------------------------------------------
echo   This window stays open so you can read the failure info.
echo   Press any key to close it.
echo.
pause >nul
exit /b 1

:server_ok
echo.
echo   [OK] PPTP is running -> http://127.0.0.1:8000
echo   Opening browser... this window will close automatically.
echo   (The server keeps running in the PPTP-Server window.)
start "" "http://127.0.0.1:8000"
timeout /t 2 /nobreak >nul
exit /b 0

:no_python
echo   [FAIL] python not found in PATH or common locations
echo          Tried: where python, common conda/env paths
echo          Install Python 3.10+ and ensure python.exe is reachable
echo.
echo   Press any key to close this window.
pause >nul
exit /b 1
