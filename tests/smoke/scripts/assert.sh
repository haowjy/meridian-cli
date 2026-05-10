#!/usr/bin/env bash
# Assertion helpers for smoke test scripts.
# Source after setup.sh.

_SMOKE_PASS=0
_SMOKE_FAIL=0

# assert_exit EXPECTED ACTUAL [label]
assert_exit() {
  local expected="$1" actual="$2" label="${3:-exit code}"
  if [[ "$actual" == "$expected" ]]; then
    echo "  PASS  $label: exit $actual"
    (( _SMOKE_PASS++ )) || true
  else
    echo "  FAIL  $label: expected exit $expected, got $actual"
    (( _SMOKE_FAIL++ )) || true
  fi
}

# assert_contains HAYSTACK NEEDLE [label]
assert_contains() {
  local haystack="$1" needle="$2" label="${3:-contains '$2'}"
  if [[ "$haystack" == *"$needle"* ]]; then
    echo "  PASS  $label"
    (( _SMOKE_PASS++ )) || true
  else
    echo "  FAIL  $label: '$needle' not found in output"
    (( _SMOKE_FAIL++ )) || true
  fi
}

# assert_not_contains HAYSTACK NEEDLE [label]
assert_not_contains() {
  local haystack="$1" needle="$2" label="${3:-not contains '$2'}"
  if [[ "$haystack" != *"$needle"* ]]; then
    echo "  PASS  $label"
    (( _SMOKE_PASS++ )) || true
  else
    echo "  FAIL  $label: '$needle' found in output (expected absent)"
    (( _SMOKE_FAIL++ )) || true
  fi
}

# assert_json FIELD EXPECTED JSON [label]
# FIELD: dot-separated key path, e.g. "status" or "model_selection.requested_token"
# Uses python3 for portability — no jq dependency.
assert_json() {
  local field="$1" expected="$2" json="$3" label="${4:-json.$field == $expected}"
  local actual
  actual=$(python3 - "$json" "$field" <<'PYEOF'
import sys, json
data = json.loads(sys.argv[1])
for key in sys.argv[2].split('.'):
    data = data[key]
print(data)
PYEOF
  ) || actual="__error__"
  if [[ "$actual" == "$expected" ]]; then
    echo "  PASS  $label"
    (( _SMOKE_PASS++ )) || true
  else
    echo "  FAIL  $label: expected '$expected', got '$actual'"
    (( _SMOKE_FAIL++ )) || true
  fi
}

# assert_file_exists PATH [label]
assert_file_exists() {
  local path="$1" label="${2:-file exists: $1}"
  if [[ -f "$path" ]]; then
    echo "  PASS  $label"
    (( _SMOKE_PASS++ )) || true
  else
    echo "  FAIL  $label: file not found"
    (( _SMOKE_FAIL++ )) || true
  fi
}

# smoke_summary — print pass/fail totals; returns 1 if any failures
smoke_summary() {
  local total=$(( _SMOKE_PASS + _SMOKE_FAIL ))
  echo ""
  echo "Smoke: $total checks, $_SMOKE_PASS passed, $_SMOKE_FAIL failed."
  if [[ "$_SMOKE_FAIL" -gt 0 ]]; then
    return 1
  fi
}
