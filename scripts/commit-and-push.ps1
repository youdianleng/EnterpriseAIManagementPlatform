<#
.SYNOPSIS
    Commit the current work and push it to origin.

.DESCRIPTION
    One call per finished ticket. Requires a clean reviewed working tree; the
    commit message comes from a file so callers avoid shell quoting problems.

    If no git credentials are configured the commit still lands locally and the
    script reports the push as pending instead of failing the whole run.

.PARAMETER MessageFile
    Path to a file containing the commit message.

.PARAMETER DryRun
    Show what would be committed without committing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MessageFile,

    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $MessageFile)) {
    throw "Commit message file not found: $MessageFile"
}

Push-Location (Join-Path $PSScriptRoot '..')
try {
    git add -A
    if ($LASTEXITCODE -ne 0) { throw 'git add failed' }

    $staged = git diff --cached --name-only
    if (-not $staged) {
        Write-Host 'Nothing staged; skipping commit.' -ForegroundColor Yellow
        return
    }

    if ($DryRun) {
        Write-Host "Would commit $($staged.Count) file(s):" -ForegroundColor Cyan
        $staged | ForEach-Object { Write-Host "  $_" }
        return
    }

    git commit -q -F $MessageFile
    if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }

    $subject = (Get-Content $MessageFile -TotalCount 1)
    Write-Host "Committed: $subject" -ForegroundColor Green

    # A push needs credentials; report rather than fail when they are missing.
    # git writes progress to stderr, so the exit code decides, never the output.
    $env:GIT_TERMINAL_PROMPT = '0'
    git push origin HEAD
    if ($LASTEXITCODE -eq 0) {
        Write-Host 'Pushed to origin.' -ForegroundColor Green
    }
    else {
        Write-Host 'PUSH PENDING: commit is local only. Configure credentials, then run: git push origin HEAD' -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
