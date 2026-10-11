# Developer check entry point. Keep it aligned with the routine preflight gate.
#
# Usage:
#   scripts\check.ps1

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot  = Split-Path -Parent $ScriptDir

Set-Location $RepoRoot

& (Join-Path $ScriptDir 'preflight.ps1') fast
if ($LASTEXITCODE -ne 0) { throw "Preflight failed" }
