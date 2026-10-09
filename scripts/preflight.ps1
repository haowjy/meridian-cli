# Preflight gate — same checks as preflight.sh.
#
# Usage:
#   scripts\preflight.ps1             # Fast prepared-environment gate
#   scripts\preflight.ps1 fast        # Explicit fast mode
#   scripts\preflight.ps1 extended    # Full opt-in gate
#   scripts\preflight.ps1 full        # Compatibility alias for extended

[CmdletBinding()]
param(
    [ValidateSet('fast', 'extended', 'full')]
    [string]$Mode = 'fast'
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
        Invoke-Step uv, run, --extra, dev, python, '-m', meridian.dev.preflight
    }
    { $_ -in @('extended', 'full') } {
        [Console]::Error.WriteLine('preflight: extended gate (explicit tests/ collection)')
        Remove-Item Env:PYTEST_ADDOPTS -ErrorAction SilentlyContinue
        Remove-Item Env:PYTESTS_LAST_FAILED -ErrorAction SilentlyContinue
        Invoke-Step uv, run, ruff, check, '.'
        Invoke-Step uv, run, --extra, dev, python, '-m', pyright
        Push-Location (Join-Path $RootDir 'src\meridian\pi_runtime')
        try {
            Invoke-Step pnpm, install, --frozen-lockfile, '--config.confirmModulesPurge=false'
            Invoke-Step pnpm, run, build:extensions
        } finally {
            Pop-Location
        }
        Invoke-Step uv, run, --extra, dev, pytest, 'tests/'
        Invoke-Step uv, build, '--no-sources'
    }
}
