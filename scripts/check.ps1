# Pre-push gate script — runs lint, type check, and fast test suite.
# Target: < 2 minutes on typical hardware.
#
# Usage:
#   scripts\check.ps1          # Full pre-push check
#   scripts\check.ps1 -quick   # Skip smoke tests (faster)

[CmdletBinding()]
param(
    [switch]$quick
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

Write-Host "==> Linting..."
uv run --extra dev ruff check .
if ($LASTEXITCODE -ne 0) { throw "Linting failed" }

Write-Host "==> Type checking..."
uv run --extra dev python -m pyright
if ($LASTEXITCODE -ne 0) { throw "Type checking failed" }

Write-Host "==> Running unit tests..."
uv run --extra dev pytest tests/unit/ -q
if ($LASTEXITCODE -ne 0) { throw "Unit tests failed" }

Write-Host "==> Running contract tests..."
uv run --extra dev pytest tests/contract/ -q
if ($LASTEXITCODE -ne 0) { throw "Contract tests failed" }

if ($quick) {
    Write-Host "==> Skipping smoke tests (--quick mode)"
} else {
    Write-Host "==> Running smoke tests..."
    uv run --extra dev pytest tests/smoke/ -q
    if ($LASTEXITCODE -ne 0) { throw "Smoke tests failed" }
}

Write-Host ""
Write-Host "✓ All checks passed"
