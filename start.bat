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
echo.

REM ---- Start server in background (minimized so no extra popup window) ----
start "PPTP-Server" /MIN cmd /c "cd /d %ROOT_DIR% && "%PYTHON%" -u -m uvicorn server:app --host 127.0.0.1 --port 8000 1> %ROOT_DIR%logs\server.log 2>&1"

REM ---- Wait for port ----
set "i=0"
:wait_port
if %i% geq 15 goto :wait_done
timeout /t 1 /nobreak >nul
netstat -ano | findstr ":8000 " >nul 2>&1
if not errorlevel 1 (
    echo   [OK] Server ready
    goto :open_browser
)
set /a i+=1
goto :wait_port
:wait_done
echo   [WARN] Server not ready after 15s, see logs\server.log

:open_browser
echo.
echo ============================================
echo   PPTP is running
echo ============================================
echo.
echo   Open in browser: http://127.0.0.1:8000
echo   API docs:        http://127.0.0.1:8000/docs
echo   Server log:      logs\server.log
echo.
echo   To stop PPTP: run stop.bat
echo.

REM Open browser directly - port is already confirmed listening
start "" "http://127.0.0.1:8000"

goto :end

:no_python
echo   [FAIL] python not found in PATH or common locations
echo          Tried: where python, common conda/env paths
echo          Install Python 3.10+ and ensure python.exe is reachable

:end
echo.
echo Press any key to close this window
echo (server will keep running in background if started)
echo.
pause >nul
exit /b 0