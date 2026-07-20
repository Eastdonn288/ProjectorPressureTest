@echo off
REM ==========================================================
REM PPTP shutdown (Windows)
REM   1) Try graceful shutdown via /api/server/shutdown
REM      -> server will cascade-kill running script subprocesses
REM   2) Fall back to force-kill uvicorn if port still occupied
REM   3) Verify port 8000 is free, report
REM ==========================================================

chcp 65001 >nul 2>&1

echo.
echo ============================================
echo   PPTP shutdown
echo ============================================
echo.

REM ---------- Step 1: graceful ----------
echo [1/3] Requesting graceful shutdown...
curl -s -m 3 -X POST http://127.0.0.1:8000/api/server/shutdown >nul 2>&1
if errorlevel 1 (
    echo   server not reachable, skipping graceful
) else (
    echo   graceful shutdown sent, waiting 3s...
    timeout /t 3 /nobreak >nul
)

REM ---------- Step 2: verify / force ----------
echo.
echo [2/3] Checking port 8000...
netstat -ano | findstr "127.0.0.1:8000" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo   port still occupied, force-killing uvicorn...
    for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000 "') do (
        if not "%%P"=="0" (
            echo   killing PID %%P...
            taskkill /F /PID %%P >nul 2>&1
        )
    )
    timeout /t 1 /nobreak >nul
) else (
    echo   port 8000 already free
)

REM ---------- Step 3: confirm ----------
echo.
echo [3/3] Verifying...
netstat -ano | findstr "127.0.0.1:8000" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo   OK - PPTP fully stopped
) else (
    echo   WARN - port 8000 still occupied, manual cleanup may be needed:
    netstat -ano | findstr ":8000"
)

echo.
echo ============================================
echo   Done. You may close this window.
echo ============================================
echo.
pause