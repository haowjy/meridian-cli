# Preflight gate — same checks as preflight.sh.
#
# Usage:
#   scripts\preflight.ps1          # Full preflight (default)
#   scripts\preflight.ps1 full     # Explicit full mode
#   scripts\preflight.ps1 fast     # Lint only (faster)

[CmdletBinding()]
param(
    [ValidateSet('fast', 'full')]
    [string]$Mode = 'full'
)

$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

function Invoke-Step {
    param([string[]]$Command)
    [Console]::Error.WriteLine("preflight: $($Command -join ' ')")
    $exe, $rest = $Command
    & $exe @rest
    if ($LASTEXITCODE -ne 0) { throw "Step failed: $($Command -join ' ')" }
}

Set-Location $RootDir

switch ($Mode) {
    'fast' {
        Invoke-Step uv, run, ruff, check, '.'
    }
    'full' {
        Invoke-Step uv, run, ruff, check, '.'
        Invoke-Step uv, run, pyright
        Invoke-Step uv, run, pytest, '-x', '-q'
        Invoke-Step uv, build, '--no-sources'
    }
}
