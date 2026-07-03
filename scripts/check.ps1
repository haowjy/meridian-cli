# Pre-push gate script — runs lint, type check, unit tests, and contracts.
# Target: < 2 minutes on typical hardware.
#
# Usage:
#   scripts\check.ps1

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

Write-Host "==> Manual smoke guides live in tests/smoke/ (not pytest-collected)."

Write-Host ""
Write-Host "✓ All checks passed"
