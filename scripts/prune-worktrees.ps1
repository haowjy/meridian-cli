#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Prune merged git worktrees and their branches.
.DESCRIPTION
    Lists worktrees whose branches are fully merged to main,
    confirms with the user (unless -Yes), then removes them.
.PARAMETER DryRun
    Show what would be pruned without making changes.
.PARAMETER Yes
    Skip confirmation prompt.
#>
[CmdletBinding()]
param(
    [Alias('dry-run')]
    [switch]$DryRun,

    [switch]$Yes,

    [Alias('h', 'help')]
    [switch]$Help,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

function Show-Usage {
    @"
Usage:
  scripts/prune-worktrees.ps1 [--dry-run|-DryRun] [--yes|-Yes]

Options:
  --dry-run, -DryRun   Show merged worktrees that would be pruned; make no changes.
  --yes, -Yes          Skip confirmation prompt and prune immediately.
  -h, --help           Show this help.
"@ | Write-Host
}

foreach ($arg in $RemainingArgs) {
    switch ($arg) {
        '--dry-run' {
            $DryRun = $true
            continue
        }
        '--yes' {
            $Yes = $true
            continue
        }
        '-h' {
            $Help = $true
            continue
        }
        '--help' {
            $Help = $true
            continue
        }
        default {
            Write-Warning "unknown argument: $arg"
            Show-Usage
            exit 2
        }
    }
}

if ($Help) {
    Show-Usage
    exit 0
}

function Derive-Slug {
    param([string]$Branch)

    if ($Branch.StartsWith('feature/')) {
        return $Branch.Substring('feature/'.Length)
    }

    if ($Branch.StartsWith('work/')) {
        return $Branch.Substring('work/'.Length)
    }

    if ($Branch.StartsWith('wt/')) {
        return $Branch.Substring('wt/'.Length)
    }

    return ''
}

Set-Location $RootDir

& git rev-parse --git-dir *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "not a git repository: $RootDir"
    exit 1
}

& git show-ref --verify --quiet refs/heads/main *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Warning "missing local 'main' branch; cannot determine merged worktrees"
    exit 1
}

$worktreePaths = New-Object System.Collections.Generic.List[string]
$worktreeBranches = New-Object System.Collections.Generic.List[string]
$currentPath = ''
$currentBranch = ''

function Flush-Record {
    if (-not [string]::IsNullOrEmpty($script:currentPath)) {
        $script:worktreePaths.Add($script:currentPath)
        $script:worktreeBranches.Add($script:currentBranch)
    }

    $script:currentPath = ''
    $script:currentBranch = ''
}

$porcelainLines = & git worktree list --porcelain
if ($LASTEXITCODE -ne 0) {
    throw 'failed to list git worktrees'
}

foreach ($line in ($porcelainLines + '')) {
    if ([string]::IsNullOrEmpty($line)) {
        Flush-Record
        continue
    }

    if ($line.StartsWith('worktree ')) {
        $currentPath = $line.Substring('worktree '.Length)
        continue
    }

    if ($line.StartsWith('branch refs/heads/')) {
        $currentBranch = $line.Substring('branch refs/heads/'.Length)
    }
}

if ($worktreePaths.Count -eq 0) {
    Write-Host 'No worktrees found.'
    exit 0
}

$primaryWorktreePath = $worktreePaths[0]

$mergedBranches = & git branch "--format=%(refname:short)" --merged main
if ($LASTEXITCODE -ne 0) {
    throw "failed to list merged branches"
}

$mergedSet = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
foreach ($branch in $mergedBranches) {
    if (-not [string]::IsNullOrEmpty($branch)) {
        [void]$mergedSet.Add($branch)
    }
}

$candidatePaths = New-Object System.Collections.Generic.List[string]
$candidateBranches = New-Object System.Collections.Generic.List[string]
$candidateSlugs = New-Object System.Collections.Generic.List[string]

for ($i = 0; $i -lt $worktreePaths.Count; $i++) {
    $path = $worktreePaths[$i]
    $branch = $worktreeBranches[$i]

    if ($path -eq $primaryWorktreePath) {
        continue
    }

    if ([string]::IsNullOrEmpty($branch)) {
        continue
    }

    if ($branch -eq 'main') {
        continue
    }

    if ($mergedSet.Contains($branch)) {
        $candidatePaths.Add($path)
        $candidateBranches.Add($branch)
        $candidateSlugs.Add((Derive-Slug -Branch $branch))
    }
}

if ($candidatePaths.Count -eq 0) {
    Write-Host 'No merged non-main worktrees to prune.'
    exit 0
}

Write-Host 'Merged worktrees eligible for pruning:'
for ($i = 0; $i -lt $candidatePaths.Count; $i++) {
    $slug = $candidateSlugs[$i]
    if ([string]::IsNullOrEmpty($slug)) {
        $slug = '(n/a)'
    }

    Write-Host "  - worktree: $($candidatePaths[$i])"
    Write-Host "    branch:   $($candidateBranches[$i])"
    Write-Host "    slug:     $slug"
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'Dry run only; no changes made.'
    exit 0
}

if (-not $Yes) {
    $response = Read-Host "`nProceed with pruning $($candidatePaths.Count) merged worktree(s)? [y/N]"
    if ($response -notmatch '^(?i:y|yes)$') {
        Write-Host 'Aborted. No changes made.'
        exit 0
    }
}

if (-not (Get-Command meridian -ErrorAction SilentlyContinue)) {
    Write-Warning 'meridian CLI not found; refusing to prune without work-item reconciliation'
    exit 1
}

$failures = 0

for ($i = 0; $i -lt $candidatePaths.Count; $i++) {
    $path = $candidatePaths[$i]
    $branch = $candidateBranches[$i]
    $slug = $candidateSlugs[$i]

    Write-Host ''
    Write-Host "Pruning: $path ($branch)"

    if ([string]::IsNullOrEmpty($slug)) {
        Write-Warning "no work-item slug derived from branch '$branch'; refusing to prune"
        $failures++
        continue
    }

    if (Test-Path -Path $path -PathType Container) {
        & git worktree remove $path
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "failed to remove worktree: $path"
            $failures++
            continue
        }
    } else {
        Write-Host "Worktree already removed: $path"
    }

    & git show-ref --verify --quiet "refs/heads/$branch" *> $null
    if ($LASTEXITCODE -eq 0) {
        & git branch -d $branch
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "failed to delete branch safely: $branch"
            $failures++
            continue
        }
    } else {
        Write-Host "Branch already deleted: $branch"
    }

    & meridian work done $slug *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "meridian work done failed for slug '$slug' (git cleanup succeeded)"
    }
}

Write-Host ''
if ($failures -eq 0) {
    Write-Host 'Prune complete.'
    exit 0
}

Write-Warning "prune completed with $failures failure(s)"
exit 1
