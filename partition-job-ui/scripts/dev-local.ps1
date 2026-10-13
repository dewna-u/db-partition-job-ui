# PartOps local laptop launcher (Windows).
# Starts API + Next.js UI. Scheduler starts ONLY with -IncludeScheduler and
# after safe-local DB checks (never 172.20.224.68 / ukbodev).
#
# Usage (from partition-job-ui root):
#   .\scripts\dev-local.ps1
#   .\scripts\dev-local.ps1 -IncludeScheduler   # requires safe .env.realtime

[CmdletBinding()]
param(
    [switch]$IncludeScheduler,
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$NodeBin = "C:\Program Files\nodejs"
if (Test-Path "$NodeBin\node.exe") {
    $env:Path = "$NodeBin;" + $env:Path
}

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Missing .venv. Create with: python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
}

function Test-ForbiddenDbTarget {
    param([string]$HostName, [string]$DbName)
    $badHosts = @("172.20.224.68", "ukbodev")
    $h = ($HostName | ForEach-Object { "$_".Trim().ToLowerInvariant() })
    $d = ($DbName | ForEach-Object { "$_".Trim().ToLowerInvariant() })
    foreach ($b in $badHosts) {
        if ($h -eq $b -or $d -eq $b -or $h.Contains($b) -or $d.Contains($b)) {
            return $true
        }
    }
    return $false
}

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $parts = $line.Split('=', 2)
        if ($parts[0].Trim() -eq $Key) {
            return $parts[1].Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

Write-Host "PartOps local launcher"
Write-Host "  UI  http://localhost:8501"
Write-Host "  API http://127.0.0.1:8001"
Write-Host "  Scheduler control http://127.0.0.1:8765 (optional)"

# Warn if API .env points at forbidden targets (do not print passwords).
$apiEnv = Join-Path $Root ".env"
if (Test-Path $apiEnv) {
    $apiHost = Get-DotEnvValue $apiEnv "DB_HOST"
    $apiDb = Get-DotEnvValue $apiEnv "DB_NAME"
    Write-Host "  API .env DB_HOST=$apiHost DB_NAME=$apiDb"
    if (Test-ForbiddenDbTarget $apiHost $apiDb) {
        Write-Warning "API .env appears to target a company/production DB. DB-backed API routes will hit that host."
        Write-Warning "Health endpoint is still OK; do not use local scheduler against this DB."
    }
} else {
    Write-Host "  API .env missing — health works; DB routes will fail until a SAFE local .env exists."
}

$api = Start-Process -FilePath $Py -ArgumentList @(
    "-m", "uvicorn", "api.main:app",
    "--host", "127.0.0.1",
    "--port", "8001",
    "--reload"
) -WorkingDirectory $Root -PassThru -WindowStyle Normal

Write-Host "Started API PID=$($api.Id)"

$ui = $null
if (-not $SkipFrontend) {
    $npm = Join-Path $NodeBin "npm.cmd"
    if (-not (Test-Path $npm)) { $npm = "npm" }
    $ui = Start-Process -FilePath $npm -ArgumentList @(
        "run", "dev", "--", "-H", "127.0.0.1", "-p", "8501"
    ) -WorkingDirectory (Join-Path $Root "frontend") -PassThru -WindowStyle Normal
    Write-Host "Started UI PID=$($ui.Id)"
}

$sched = $null
if ($IncludeScheduler) {
    $rt = Join-Path $Root ".env.realtime"
    if (-not (Test-Path $rt)) {
        Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
        if ($ui) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
        throw "Missing .env.realtime — refuse to start scheduler."
    }
    $h = Get-DotEnvValue $rt "DB_HOST"
    $d = Get-DotEnvValue $rt "DB_NAME"
    $e = Get-DotEnvValue $rt "REALTIME_DB_EXPECTED_NAME"
    Write-Host "  Scheduler .env.realtime DB_HOST=$h DB_NAME=$d REALTIME_DB_EXPECTED_NAME=$e"
    if (Test-ForbiddenDbTarget $h $d) {
        Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
        if ($ui) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
        throw "Refusing scheduler: DB_HOST/DB_NAME looks like production (172.20.224.68 / ukbodev)."
    }
    if ([string]::IsNullOrWhiteSpace($h) -or [string]::IsNullOrWhiteSpace($d) -or [string]::IsNullOrWhiteSpace($e)) {
        Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
        if ($ui) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
        throw "Refusing scheduler: DB_HOST, DB_NAME, and REALTIME_DB_EXPECTED_NAME are required."
    }
    if ($d -ne $e) {
        Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
        if ($ui) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
        throw "Refusing scheduler: DB_NAME must equal REALTIME_DB_EXPECTED_NAME."
    }
    if ($h -notin @("127.0.0.1", "localhost", "::1")) {
        Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
        if ($ui) { Stop-Process -Id $ui.Id -Force -ErrorAction SilentlyContinue }
        throw "Refusing scheduler: local launcher only allows DB_HOST 127.0.0.1/localhost for now."
    }
    $sched = Start-Process -FilePath $Py -ArgumentList @(
        "-m", "scheduler_backend.main", "--log-level", "INFO"
    ) -WorkingDirectory $Root -PassThru -WindowStyle Normal
    Write-Host "Started scheduler PID=$($sched.Id)"
} else {
    Write-Host "Scheduler NOT started (pass -IncludeScheduler only with a safe local DB)."
}

Write-Host ""
Write-Host "Verify:"
Write-Host "  curl http://127.0.0.1:8001/api/health"
Write-Host "  curl http://127.0.0.1:8501/"
Write-Host "Stop: close the spawned windows or Stop-Process -Id <pids>"
Write-Host "PIDs: api=$($api.Id) ui=$($ui.Id) scheduler=$($sched.Id)"
