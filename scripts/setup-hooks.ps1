$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not (Test-Path "$root\.githooks")) {
    Write-Error ".githooks\ directory not found."
    exit 1
}
git -C $root config --local core.hooksPath .githooks
Write-Host "Git hooks activated: core.hooksPath = .githooks"
Write-Host "Active hook: pre-push (fast preflight + tag policy)"
Write-Host "Run scripts\preflight.ps1 extended explicitly for the full release gate."
Write-Host "Optional hook: pre-commit (fast ruff check) lives at .githooks/optional/pre-commit and is not active by default"
