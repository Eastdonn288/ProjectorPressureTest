# server_window.ps1 - launched by start.bat inside the "PPTP-Server" window.
#
# Responsibilities:
#   1. Start uvicorn in the background (stdout/stderr -> logs\server.out.log /
#      logs\server.err.log)
#   2. Echo both log files to this window in real time
#   3. When uvicorn exits, show the exit info and close the window after 8s
#   4. -NoNewWindow keeps uvicorn attached to this console, so closing this
#      window also stops the server
#
# Usage: powershell -NoProfile -ExecutionPolicy Bypass -File server_window.ps1 "<python path>"

param(
    [string]$Python = "python",
    [string]$Root   = $PSScriptRoot
)

$ErrorActionPreference = "Continue"

$logsDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $logsDir | Out-Null

$stdoutLog = Join-Path $logsDir "server.out.log"
$stderrLog = Join-Path $logsDir "server.err.log"
Remove-Item $stdoutLog, $stderrLog -ErrorAction SilentlyContinue

# Make sure Python emits UTF-8 even when stdout is redirected to a file/pipe.
$env:PYTHONIOENCODING = "utf-8"

$proc = Start-Process -FilePath $Python `
    -ArgumentList @('-u', '-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', '8000') `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError  $stderrLog `
    -NoNewWindow `
    -PassThru

Write-Host ""
Write-Host "  PPTP server starting (pid $($proc.Id)) ..."
Write-Host "  Live output below. Close this window to stop the server."
Write-Host "  (also logged: logs\server.out.log / logs\server.err.log)"
Write-Host ""

# Poll both log files and echo new lines; stop once uvicorn exits.
$seen = 0
while (-not $proc.HasExited) {
    $all = @(Get-Content -Path $stdoutLog, $stderrLog -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($all.Count -ge $seen) {
        $new = @($all | Select-Object -Skip $seen)
        $seen = $all.Count
        foreach ($line in $new) { Write-Host $line }
    }
    Start-Sleep -Milliseconds 300
}

# Flush any lines that arrived right before exit.
$all = @(Get-Content -Path $stdoutLog, $stderrLog -Encoding UTF8 -ErrorAction SilentlyContinue)
if ($all.Count -ge $seen) {
    foreach ($line in ($all | Select-Object -Skip $seen)) { Write-Host $line }
}

Write-Host ""
Write-Host "  [PPTP] server exited (pid $($proc.Id), exit code $($proc.ExitCode))."
Write-Host "  Window will close in 8 seconds..."
Start-Sleep -Seconds 8
