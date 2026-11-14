<#
.SYNOPSIS
Takes a verified pg_dump backup of the LOCAL demo database.

.DESCRIPTION
Dumps inside the postgres container (so PowerShell never re-encodes the binary), checks that the archive is readable,
copies it out, proves the copy is byte-identical by SHA-256, and writes a manifest next to it. Each backup gets its own
file name; nothing is overwritten and no variable holding the path is reused by later steps. Prints the backup path.

Run from the repository root. This script does not stop or change any service.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path (Get-Location) 'backups'),
    [string]$Label = 'dcim-before-demo-loader',
    [string[]]$ComposeFile = @('docker-compose.yml')
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'DemoCommon.ps1')

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$fileName = "$Label-$stamp.dump"
$hostPath = Join-Path $OutputDirectory $fileName
if (Test-Path -LiteralPath $hostPath) { throw "Refusing to overwrite an existing backup: $hostPath" }
$inContainer = "/tmp/$fileName"

Invoke-Postgres -What 'pg_dump' -ComposeFile $ComposeFile -Script ('pg_dump -U postgres -Fc -d "$POSTGRES_DB" -f ' + $inContainer) | Out-Null
$listing = Invoke-Postgres -What 'pg_restore --list (archive readable)' -ComposeFile $ComposeFile -Script ('pg_restore --list ' + $inContainer)
if (-not ($listing | Where-Object { $_ -match 'TABLE DATA' })) { throw 'The archive lists no table data; refusing to call it a backup.' }
$containerHash = (Invoke-Postgres -What 'sha256sum (container)' -ComposeFile $ComposeFile -Script ('sha256sum ' + $inContainer) | Select-Object -First 1).ToString().Split(' ')[0]

Invoke-Compose -What 'docker compose cp (backup out of the container)' -ComposeFile $ComposeFile -Arguments @('cp', "postgres:$inContainer", $hostPath) | Out-Null
if (-not (Test-Path -LiteralPath $hostPath -PathType Leaf)) { throw "The copy did not create $hostPath" }
$item = Get-Item -LiteralPath $hostPath
if ($item.Length -le 0) { throw "The copied backup is empty: $hostPath" }
$hostHash = (Get-FileHash -LiteralPath $hostPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hostHash -ne $containerHash.ToLowerInvariant()) { throw "Checksum mismatch after copy (container $containerHash, host $hostHash). Do not use $hostPath." }

"$hostHash  $fileName" | Set-Content -LiteralPath "$hostPath.sha256" -Encoding ascii
Invoke-Postgres -What 'remove the temporary dump in the container' -ComposeFile $ComposeFile -Script ('rm -f ' + $inContainer) | Out-Null
Write-Information "Backup verified: $hostPath ($($item.Length) bytes, sha256 $hostHash)" -InformationAction Continue
$hostPath
