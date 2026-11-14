# Shared helpers for the local demo scripts. Dot-source it: . "$PSScriptRoot\DemoCommon.ps1"
# LOCAL DEMONSTRATION DATABASE ONLY.

Set-StrictMode -Version Latest

function Invoke-Native {
    <#
    .SYNOPSIS
    Runs a native command and THROWS if it exits non-zero. PowerShell does not stop on a failing native command by itself
    ($ErrorActionPreference does not apply to exit codes), so every docker call goes through here.
    #>
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    # Windows PowerShell 5.1 turns a native command's stderr text into a terminating error when $ErrorActionPreference is
    # 'Stop', and docker writes progress to stderr. Judge the command by its exit code only, inside this function.
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    $output = & $Command 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE): $($output | Out-String)"
    }
    return $output
}

function Get-ComposeArguments {
    param([Parameter(Mandatory)][string[]]$ComposeFile)
    foreach ($file in $ComposeFile) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Compose file not found: $file (run from the repository root)" }
        '-f'
        $file
    }
}

function Invoke-Compose {
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][string[]]$ComposeFile,
        [Parameter(Mandatory)][string[]]$Arguments
    )
    $compose = @(Get-ComposeArguments -ComposeFile $ComposeFile)
    Invoke-Native -What $What -Command { docker compose @compose @Arguments }
}

function Invoke-Postgres {
    <# Runs a shell command inside the postgres container; $POSTGRES_DB is expanded THERE, not by PowerShell. #>
    param(
        [Parameter(Mandatory)][string]$What,
        [Parameter(Mandatory)][string[]]$ComposeFile,
        [Parameter(Mandatory)][string]$Script
    )
    Invoke-Compose -What $What -ComposeFile $ComposeFile -Arguments @('exec', '-T', 'postgres', 'sh', '-c', $Script)
}
