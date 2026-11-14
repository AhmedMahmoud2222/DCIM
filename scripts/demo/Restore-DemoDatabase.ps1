<#
.SYNOPSIS
Restores the LOCAL demo database from a backup made by Backup-DemoDatabase.ps1, after proving the backup is good.

.DESCRIPTION
Nothing destructive happens until every check has passed:
  1. -ConfirmDestroyLocalDemoDatabase was given, the backup file and its .sha256 manifest exist, the checksum matches;
  2. every service that can write is stopped (including the demo-refresh override service) and no application session
     is left on the database;
  3. the archive is copied into the container, its checksum is re-checked THERE, and it is test-restored into a throwaway
     database that is then dropped.
Only then are the real database dropped, recreated and restored. Any failure before step 3 ends the script with the
database untouched. Services are NOT restarted: rebuild the images from the commit that matches the restored schema first.

Run from the repository root.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BackupPath,
    [switch]$ConfirmDestroyLocalDemoDatabase,
    [string[]]$ComposeFile = @('docker-compose.yml', 'docker-compose.demo.yml')
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'DemoCommon.ps1')

if (-not $ConfirmDestroyLocalDemoDatabase) { throw 'This replaces the local demo database. Re-run with -ConfirmDestroyLocalDemoDatabase.' }
if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) { throw "Backup not found: $BackupPath" }
$backup = Get-Item -LiteralPath $BackupPath
if ($backup.Length -le 0) { throw "Backup is empty: $BackupPath" }
$manifestPath = "$($backup.FullName).sha256"
if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw "No manifest next to the backup ($manifestPath). Use a backup made by Backup-DemoDatabase.ps1." }
$expected = ((Get-Content -LiteralPath $manifestPath -TotalCount 1) -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash -LiteralPath $backup.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "Backup checksum does not match its manifest (manifest $expected, file $actual)." }

# Stop every writer, then prove none is left.
Invoke-Compose -What 'stop demo-refresh and the application services' -ComposeFile $ComposeFile -Arguments @('stop', 'demo-refresh', 'celery-worker', 'celery-beat', 'backend') | Out-Null
$sessions = Invoke-Postgres -What 'count application sessions' -ComposeFile $ComposeFile -Script `
    'psql -U postgres -d "$POSTGRES_DB" -Atc "select count(*) from pg_stat_activity where datname = current_database() and usename = ''dcim_app'' and pid <> pg_backend_pid()"'
if ([int]($sessions | Select-Object -First 1) -ne 0) { throw "Application sessions are still connected ($sessions). Stop whatever holds them and run this again." }

# Copy in, verify there, test-restore into a throwaway database.
$guid = [guid]::NewGuid().ToString('N')
$inContainer = "/tmp/restore-$guid.dump"
$scratch = "dcim_restore_check_$guid"
Invoke-Compose -What 'docker compose cp (backup into the container)' -ComposeFile $ComposeFile -Arguments @('cp', $backup.FullName, "postgres:$inContainer") | Out-Null
$containerHash = (Invoke-Postgres -What 'sha256sum (container)' -ComposeFile $ComposeFile -Script ('sha256sum ' + $inContainer) | Select-Object -First 1).ToString().Split(' ')[0].ToLowerInvariant()
if ($containerHash -ne $expected) { throw "The copy inside the container does not match the manifest (container $containerHash, manifest $expected)." }
$listing = Invoke-Postgres -What 'pg_restore --list' -ComposeFile $ComposeFile -Script ('pg_restore --list ' + $inContainer)
if (-not ($listing | Where-Object { $_ -match 'TABLE DATA' })) { throw 'The archive lists no table data.' }
Invoke-Postgres -What 'create the throwaway check database' -ComposeFile $ComposeFile -Script ('createdb -U postgres ' + $scratch) | Out-Null
try {
    Invoke-Postgres -What 'test restore into the throwaway database' -ComposeFile $ComposeFile -Script ('pg_restore -U postgres -d ' + $scratch + ' --no-owner --exit-on-error ' + $inContainer) | Out-Null
}
finally {
    Invoke-Postgres -What 'drop the throwaway check database' -ComposeFile $ComposeFile -Script ('dropdb -U postgres --if-exists ' + $scratch) | Out-Null
}

# Every check has passed. From here the real database is replaced.
Invoke-Postgres -What 'dropdb' -ComposeFile $ComposeFile -Script 'dropdb -U postgres --if-exists "$POSTGRES_DB"' | Out-Null
Invoke-Postgres -What 'createdb' -ComposeFile $ComposeFile -Script 'createdb -U postgres "$POSTGRES_DB"' | Out-Null
Invoke-Postgres -What 'pg_restore' -ComposeFile $ComposeFile -Script ('pg_restore -U postgres -d "$POSTGRES_DB" --no-owner --exit-on-error ' + $inContainer) | Out-Null
Invoke-Compose -What 're-apply the privileged-role bootstrap' -ComposeFile $ComposeFile -Arguments @('run', '--rm', 'bootstrap-privileges') | Out-Null
Invoke-Postgres -What 'remove the temporary dump in the container' -ComposeFile $ComposeFile -Script ('rm -f ' + $inContainer) | Out-Null

$revision = Invoke-Postgres -What 'read the restored schema revision' -ComposeFile $ComposeFile -Script 'psql -U postgres -d "$POSTGRES_DB" -Atc "select version_num from alembic_version"'
$rackCount = Invoke-Postgres -What 'count racks' -ComposeFile $ComposeFile -Script 'psql -U postgres -d "$POSTGRES_DB" -Atc "select count(*) from rack"'
$equipmentCount = Invoke-Postgres -What 'count equipment' -ComposeFile $ComposeFile -Script 'psql -U postgres -d "$POSTGRES_DB" -Atc "select count(*) from equipment"'
Write-Host "Restored $($backup.Name). Schema revision: $($revision | Select-Object -First 1). $($rackCount | Select-Object -First 1) racks, $($equipmentCount | Select-Object -First 1) equipment."
Write-Host 'Services are stopped. Check out the commit that matches this revision, rebuild ALL images (docker compose build), then start them.'
