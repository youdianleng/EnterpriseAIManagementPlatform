<#
.SYNOPSIS
    Commit the current work and push it to origin.

.DESCRIPTION
    One call per finished ticket. Requires a clean reviewed working tree; the
    commit message comes from a file so callers avoid shell quoting problems.

    If no git credentials are configured the commit still lands locally and the
    script reports the push as pending instead of failing the whole run.

    Every git call goes through Invoke-Git. Git writes ordinary progress and
    line-ending notices to stderr, and PowerShell turns a native command's stderr
    into an error record, which $ErrorActionPreference='Stop' then promotes to a
    terminating error — so a successful command would report failure. Status is
    taken from the exit code alone.

.PARAMETER MessageFile
    Path to a file containing the commit message.

.PARAMETER Paths
    Stage only these paths, instead of everything. Useful when more than one
    person or agent is working in the tree at once: `git add -A` would sweep a
    colleague's half-finished work into this commit, and a commit that contains
    somebody else's unfinished changes cannot be reviewed or reverted as one
    thing.

.PARAMETER DryRun
    Show what would be committed without committing.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$MessageFile,

    [string[]]$Paths,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

function Invoke-Git {
    <#
    Run git, stream its output, and return $true when it succeeded.

    Arguments are passed as one array. PowerShell would otherwise treat a flag
    like `-A` as a parameter name for this function rather than as a git
    argument. The caller decides what a failure means; this never throws, because
    "did it work" here is the exit code and nothing else.
    #>
    param([string[]]$Arguments)

    $output = & git @Arguments 2>&1
    $succeeded = ($LASTEXITCODE -eq 0)
    foreach ($line in $output) {
        if ($line -is [System.Management.Automation.ErrorRecord]) {
            Write-Host $line.ToString()
        }
        else {
            Write-Host $line
        }
    }
    return $succeeded
}

if (-not (Test-Path $MessageFile)) {
    throw "Commit message file not found: $MessageFile"
}

Push-Location (Join-Path $PSScriptRoot '..')
try {
    $addArguments = if ($Paths) { @('add', '--') + $Paths } else { @('add', '-A') }
    if (-not (Invoke-Git -Arguments $addArguments)) {
        throw 'git add failed'
    }

    $staged = & git diff --cached --name-only
    if (-not $staged) {
        Write-Host 'Nothing staged; skipping commit.' -ForegroundColor Yellow
        return
    }

    if ($DryRun) {
        Write-Host "Would commit $($staged.Count) file(s):" -ForegroundColor Cyan
        $staged | ForEach-Object { Write-Host "  $_" }
        return
    }

    if (-not (Invoke-Git -Arguments @('commit', '-q', '-F', $MessageFile))) {
        throw 'git commit failed'
    }

    Write-Host "Committed: $(Get-Content $MessageFile -TotalCount 1)" -ForegroundColor Green

    # Credentials may be absent; a missing push is reported, not fatal.
    $env:GIT_TERMINAL_PROMPT = '0'
    if (Invoke-Git -Arguments @('push', 'origin', 'HEAD')) {
        Write-Host 'Pushed to origin.' -ForegroundColor Green
    }
    else {
        Write-Host 'PUSH PENDING: the commit is local. Run: git push origin HEAD' -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}
