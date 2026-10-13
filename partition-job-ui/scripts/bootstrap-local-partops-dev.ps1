# Bootstrap localhost-only PartOps database: partops_dev
#
# SAFETY
#   - Connects ONLY to 127.0.0.1:5432
#   - Refuses if inet_server_addr() is not loopback
#   - Never prints passwords
#   - Does NOT invent missing cron/worker function bodies
#
# Prerequisites:
#   $env:PARTOPS_LOCAL_PG_ADMIN_PASSWORD = '<local postgres admin password>'
#   Optional: $env:PARTOPS_LOCAL_PG_ADMIN_USER = 'postgres'
#
# Usage (from partition-job-ui root):
#   .\scripts\bootstrap-local-partops-dev.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$PgBin = "C:\Program Files\PostgreSQL\16\bin"
if (-not (Test-Path "$PgBin\psql.exe")) {
    throw "psql not found at $PgBin\psql.exe"
}
$env:Path = "$PgBin;" + $env:Path

$AdminUser = if ($env:PARTOPS_LOCAL_PG_ADMIN_USER) { $env:PARTOPS_LOCAL_PG_ADMIN_USER } else { "postgres" }
$AdminPassword = $env:PARTOPS_LOCAL_PG_ADMIN_PASSWORD
if ([string]::IsNullOrWhiteSpace($AdminPassword)) {
    throw @"
LOCAL PostgreSQL admin password is required.

Set a session environment variable (do not commit it):

  `$env:PARTOPS_LOCAL_PG_ADMIN_PASSWORD = '<your local postgres password>'
  `$env:PARTOPS_LOCAL_PG_ADMIN_USER = 'postgres'   # optional

Then re-run:

  .\scripts\bootstrap-local-partops-dev.ps1

Do not use production / ukbodev credentials.
"@
}

$env:PGPASSWORD = $AdminPassword
$HostName = "127.0.0.1"
$Port = "5432"
$DbName = "partops_dev"
$AppRole = "partops_local"

function Invoke-LocalPsql {
    param(
        [string]$Database,
        [string]$Sql,
        [string[]]$File
    )
    $args = @(
        "-w", "-h", $HostName, "-p", $Port, "-U", $AdminUser, "-d", $Database,
        "-v", "ON_ERROR_STOP=1"
    )
    if ($File) {
        $args += @("-f", $File)
        & psql @args
    } else {
        $args += @("-c", $Sql)
        & psql @args
    }
    if ($LASTEXITCODE -ne 0) {
        throw "psql failed (exit $LASTEXITCODE) against local database '$Database'"
    }
}

try {
    Write-Host "Proving connection is localhost..."
    $probe = & psql -w -h $HostName -p $Port -U $AdminUser -d postgres -v ON_ERROR_STOP=1 -t -A -F "|" -c @"
SELECT COALESCE(host(inet_server_addr())::text, 'local'),
       inet_server_port(),
       current_database(),
       current_user;
"@
    if ($LASTEXITCODE -ne 0) {
        throw "Cannot connect to local PostgreSQL as '$AdminUser' (password rejected or server down)."
    }
    $parts = ($probe | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Split("|")
    $addr = $parts[0]
    $portSeen = $parts[1]
    $dbSeen = $parts[2]
    $userSeen = $parts[3]
    Write-Host "  inet_server_addr=$addr inet_server_port=$portSeen current_database=$dbSeen current_user=$userSeen"
    if ($addr -notin @("127.0.0.1", "::1", "local") -and $addr -notmatch '^127\.') {
        throw "Refusing bootstrap: server address '$addr' is not localhost."
    }
    if ("$portSeen" -ne $Port) {
        throw "Refusing bootstrap: unexpected port '$portSeen'."
    }

    Write-Host "Ensuring database $DbName exists..."
    $exists = & psql -w -h $HostName -p $Port -U $AdminUser -d postgres -t -A -c "SELECT 1 FROM pg_database WHERE datname = '$DbName';"
    if (($exists | Out-String).Trim() -ne "1") {
        Invoke-LocalPsql -Database "postgres" -Sql "CREATE DATABASE $DbName;"
        Write-Host "  Created database $DbName"
    } else {
        Write-Host "  Database $DbName already exists"
    }

    # Re-prove locality on target DB
    $probe2 = & psql -w -h $HostName -p $Port -U $AdminUser -d $DbName -t -A -F "|" -c @"
SELECT COALESCE(host(inet_server_addr())::text, 'local'), current_database();
"@
    $p2 = ($probe2 | Where-Object { $_.Trim() -ne "" } | Select-Object -First 1).Split("|")
    if ($p2[1] -ne $DbName) { throw "Connected database mismatch." }
    if ($p2[0] -notin @("127.0.0.1", "::1", "local") -and $p2[0] -notmatch '^127\.') {
        throw "Refusing: target DB not on localhost ($($p2[0]))."
    }

    # App role password: generate once into gitignored env files only
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] 24
    $rng.GetBytes($bytes)
    $AppPassword = ([Convert]::ToBase64String($bytes) -replace '[+/=]', 'x')

    Write-Host "Ensuring role $AppRole exists (non-superuser)..."
    $roleExists = & psql -w -h $HostName -p $Port -U $AdminUser -d postgres -t -A -c "SELECT 1 FROM pg_roles WHERE rolname = '$AppRole';"
    if (($roleExists | Out-String).Trim() -ne "1") {
        # Password passed via psql variable to avoid shell echo in report paths
        & psql -w -h $HostName -p $Port -U $AdminUser -d postgres -v ON_ERROR_STOP=1 `
            -v "pwd=$AppPassword" `
            -c "CREATE ROLE $AppRole LOGIN PASSWORD :'pwd' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
        if ($LASTEXITCODE -ne 0) { throw "CREATE ROLE failed" }
    } else {
        & psql -w -h $HostName -p $Port -U $AdminUser -d postgres -v ON_ERROR_STOP=1 `
            -v "pwd=$AppPassword" `
            -c "ALTER ROLE $AppRole WITH LOGIN PASSWORD :'pwd' NOSUPERUSER NOCREATEDB NOCREATEROLE;"
        if ($LASTEXITCODE -ne 0) { throw "ALTER ROLE failed" }
    }
    Invoke-LocalPsql -Database "postgres" -Sql "GRANT CONNECT ON DATABASE $DbName TO $AppRole;"

    Write-Host "Applying version-controlled framework bootstrap..."
    # Session approval marker required by bootstrap script
    $bootSql = @"
CREATE TEMP TABLE _partition_realtime_migration_approved AS
SELECT now() AS approved_at, current_database() AS approved_database;
\i sql/bootstrap_partition_job_framework.sql
"@
    $bootSql | & psql -w -h $HostName -p $Port -U $AdminUser -d $DbName -v ON_ERROR_STOP=1
    if ($LASTEXITCODE -ne 0) { throw "bootstrap_partition_job_framework.sql failed" }

    Write-Host "Applying duration column migration..."
    Invoke-LocalPsql -Database $DbName -File (Join-Path $Root "sql\migrations\20260929_01_add_execution_duration_ms.sql")

    Write-Host "Checking for required non-versioned framework functions..."
    $missing = & psql -w -h $HostName -p $Port -U $AdminUser -d $DbName -t -A -c @"
SELECT string_agg(name, ', ' ORDER BY name)
FROM (
  SELECT 'cron_to_interval_or_next_run(text)' AS name
   WHERE to_regprocedure('mubasher_oms.cron_to_interval_or_next_run(text)') IS NULL
  UNION ALL
  SELECT 'create_any_table_partition(varchar,varchar,varchar,integer,interval)'
   WHERE to_regprocedure('mubasher_oms.create_any_table_partition(varchar,varchar,varchar,integer,interval)') IS NULL
  UNION ALL
  SELECT 'drop_any_table_partition(varchar,varchar,interval)'
   WHERE to_regprocedure('mubasher_oms.drop_any_table_partition(varchar,varchar,interval)') IS NULL
  UNION ALL
  SELECT 'insert_data_to_partition_job_table'
   WHERE NOT EXISTS (
     SELECT 1 FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'mubasher_oms' AND p.proname = 'insert_data_to_partition_job_table'
   )
) s;
"@
    $missingList = ($missing | Out-String).Trim()
    if ($missingList) {
        Write-Host ""
        Write-Host "PARTIAL BOOTSTRAP COMPLETE — versioned tables installed."
        Write-Host "BLOCKED: missing framework function bodies (not in Git):"
        Write-Host "  $missingList"
        Write-Host ""
        Write-Host "Provide CREATE FUNCTION bodies from a SAFE capture, or explicitly"
        Write-Host "approve LOCAL-ONLY development stubs before continuing."
        Write-Host "Realtime scheduler SQL was NOT applied yet."
    } else {
        Write-Host "All required framework functions present — applying migrations + realtime SQL..."
        Invoke-LocalPsql -Database $DbName -File (Join-Path $Root "sql\migrations\20260929_02_update_data_to_partition_job_table.sql")
        # Manual function uses DROP+CREATE — apply carefully
        Invoke-LocalPsql -Database $DbName -File (Join-Path $Root "sql\migrations\20260929_03_run_partition_job_manual_duration.sql")
        $rt = Get-Content (Join-Path $Root "sql\realtime_scheduler_v1.sql") -Raw
        # Strip/adapt role name: realtime grants partition_job_scheduler; create that role alias locally
        Invoke-LocalPsql -Database $DbName -Sql "DO `$`$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='partition_job_scheduler') THEN CREATE ROLE partition_job_scheduler NOLOGIN; END IF; END `$`$;"
        Invoke-LocalPsql -Database $DbName -File (Join-Path $Root "sql\realtime_scheduler_v1.sql")
    }

    # Grants for app role (minimal)
    Invoke-LocalPsql -Database $DbName -Sql @"
GRANT USAGE ON SCHEMA mubasher_oms TO $AppRole;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA mubasher_oms TO $AppRole;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA mubasher_oms TO $AppRole;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA mubasher_oms TO $AppRole;
ALTER DEFAULT PRIVILEGES IN SCHEMA mubasher_oms GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $AppRole;
ALTER DEFAULT PRIVILEGES IN SCHEMA mubasher_oms GRANT USAGE, SELECT ON SEQUENCES TO $AppRole;
ALTER DEFAULT PRIVILEGES IN SCHEMA mubasher_oms GRANT EXECUTE ON FUNCTIONS TO $AppRole;
"@

    # Write gitignored env files (password never echoed)
    $envApi = @"
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=$DbName
DB_USER=$AppRole
DB_PASSWORD=$AppPassword
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT=5
PARTITION_SCHEDULER_REFRESH_URL=http://127.0.0.1:8765/internal/scheduler/refresh
PARTITION_SCHEDULER_STATUS_URL=http://127.0.0.1:8765/internal/scheduler/status
PARTITION_SCHEDULER_REFRESH_TIMEOUT_SECONDS=2
"@
    $envRt = @"
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=$DbName
DB_USER=$AppRole
DB_PASSWORD=$AppPassword
DB_SSLMODE=prefer
DB_CONNECT_TIMEOUT=5
REALTIME_DB_EXPECTED_NAME=$DbName
PARTITION_SCHEDULER_LOOKAHEAD_SECONDS=120
PARTITION_SCHEDULER_RECONCILE_SECONDS=30
PARTITION_SCHEDULER_BIND_HOST=127.0.0.1
PARTITION_SCHEDULER_BIND_PORT=8765
PARTITION_JOB_SCHEMA=mubasher_oms
PARTITION_UPCOMING_JOBS_FUNCTION=get_upcoming_partition_jobs
PARTITION_SCHEDULED_RUN_FUNCTION=run_partition_job_scheduled
"@
    Set-Content -Path (Join-Path $Root ".env") -Value $envApi -Encoding UTF8
    Set-Content -Path (Join-Path $Root ".env.realtime") -Value $envRt -Encoding UTF8
    Write-Host "Wrote gitignored .env and .env.realtime (localhost / $DbName / $AppRole)."
    Write-Host "Bootstrap script finished."
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}
