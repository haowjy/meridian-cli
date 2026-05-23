# Smoke: Cursor subprocess boundary

Manual smoke for the real `cursor agent` subprocess boundary.

## Setup

```bash
REPO_ROOT=$(pwd)
. tests/smoke/scripts/setup.sh
export MERIDIAN_PROJECT_DIR="$REPO_ROOT"
```

## Dry-run alias resolution + projected Cursor command

```bash
uv run meridian spawn -m composer -p "Reply with exactly OK" --dry-run --format json
```
- [ ] Exit 0
- [ ] `status == "dry-run"`
- [ ] `harness_id == "cursor"`
- [ ] `model == "composer-2.5"`
- [ ] `model_selection.requested_token == "composer"`
- [ ] `model_selection.canonical_model_id == "composer-2.5"`
- [ ] `cli_command` contains:
  - [ ] `cursor agent --print --output-format stream-json --trust`
  - [ ] `--model composer-2.5`

```bash
uv run meridian spawn -m composer --approval auto -p "Reply with exactly OK" --dry-run --format json
```
- [ ] Exit 0
- [ ] `status == "dry-run"`
- [ ] `cli_command` contains `--force` (approval `auto` projection)

## Live happy path (real Cursor CLI account required)

Prereqs: Cursor CLI installed, logged in, and allowed to use `composer-2.5`.

```bash
OUT=$(uv run meridian spawn --harness cursor -m composer -p "Reply with exactly OK" --format json)
echo "$OUT"
SPAWN_ID=$(echo "$OUT" | uv run python -c 'import json,sys; print(json.load(sys.stdin)["spawn_id"])')
uv run meridian spawn show "$SPAWN_ID" --format json
```
- [ ] Spawn command exits 0
- [ ] Spawn JSON has `status == "succeeded"`
- [ ] `spawn show` reports `harness == "cursor"` and `model == "composer-2.5"`
- [ ] `report_body` / `report_path` present
- [ ] Usage fields (`input_tokens`, `output_tokens`, cache tokens) are present when Cursor emits usage
- [ ] Session identifier is surfaced if present in Cursor output (field may be absent on some Cursor CLI versions)

## Missing `cursor` binary / PATH diagnostics

```bash
UV_BIN="$(command -v uv)"
NO_CURSOR_BIN=$(mktemp -d)
ln -s "$UV_BIN" "$NO_CURSOR_BIN/uv"

PATH="$NO_CURSOR_BIN" MERIDIAN_PROJECT_DIR="$(pwd)" \
  "$UV_BIN" run meridian spawn --harness cursor -m composer -p "Reply with exactly OK" --timeout 1 --format json
```
- [ ] Exit non-zero
- [ ] Error is actionable (for example: harness not installed / not on `PATH`)
- [ ] No traceback

## Protocol / stream-json failure behavior

### Non-JSON first line should fail actionably

```bash
FAKE_BIN=$(mktemp -d)
cat > "$FAKE_BIN/cursor" << 'SH'
#!/usr/bin/env bash
echo 'not-json'
exit 0
SH
chmod +x "$FAKE_BIN/cursor"

MERIDIAN_HOME=$(mktemp -d) MERIDIAN_PROJECT_DIR="$(pwd)" PATH="$FAKE_BIN:$PATH" \
  uv run meridian spawn --harness cursor -m composer -p "Reply with exactly OK" --format json
```
- [ ] Ends `failed`
- [ ] `meridian spawn show <spawn_id> --format json` includes actionable protocol failure text (for example `Cursor protocol mismatch...`) or a timeout-style failure reason
- [ ] Failure is **not** a transport-registration/transport-lookup error

### No terminal `result` event note

If Cursor emits valid JSON events but no terminal `result`, spawn should still fail with an actionable timeout/failure reason (not transport lookup). Capture `spawn show --format json` evidence for review.

## Windows equivalents

Windows versions of the key POSIX smoke steps. Run in PowerShell or `cmd.exe`.

### Missing `cursor` binary (PowerShell)

```powershell
$NO_CURSOR_BIN = New-TemporaryFile | % { Remove-Item $_; New-Item -ItemType Directory -Path $_ }
$UV_BIN = (Get-Command uv).Source
Copy-Item $UV_BIN "$NO_CURSOR_BIN\uv.exe"

$env:PATH = "$NO_CURSOR_BIN;$env:PATH"
$env:MERIDIAN_PROJECT_DIR = (Get-Location).Path
uv run meridian spawn --harness cursor -m composer -p "Reply with exactly OK" --timeout 1 --format json
```
- [ ] Exit non-zero
- [ ] Error is actionable (harness not installed / not on PATH)
- [ ] No traceback

### Non-JSON first line (PowerShell)

```powershell
$FAKE_BIN = New-TemporaryFile | % { Remove-Item $_; New-Item -ItemType Directory -Path $_ }
$CURSOR_BAT = "$FAKE_BIN\cursor.bat"
Set-Content $CURSOR_BAT "@echo not-json"

$MERIDIAN_HOME = New-TemporaryFile | % { Remove-Item $_; New-Item -ItemType Directory -Path $_ }
$env:MERIDIAN_HOME = $MERIDIAN_HOME.FullName
$env:MERIDIAN_PROJECT_DIR = (Get-Location).Path
$env:PATH = "$FAKE_BIN;$env:PATH"
uv run meridian spawn --harness cursor -m composer -p "Reply with exactly OK" --format json
```
- [ ] Ends `failed`
- [ ] `meridian spawn show <spawn_id> --format json` includes actionable protocol failure text
- [ ] Failure is **not** a transport-registration/transport-lookup error
