# Adversarial Smoke Tests

These checks are intentionally noisy. Try odd inputs, race conditions, state tampering, and permission boundaries. Go beyond these suggestions and use your creativity to find bugs.

## Setup

```bash
export REPO_ROOT=/abs/path/to/meridian-channel
export SMOKE_REPO="$(mktemp -d /tmp/meridian-adversarial.XXXXXX)"
git -C "$SMOKE_REPO" init --quiet
for var in $(env | awk -F= '/^MERIDIAN_/ {print $1}'); do unset "$var"; done
export MERIDIAN_PROJECT_DIR="$SMOKE_REPO"
cd "$REPO_ROOT"
export RUNTIME_ROOT="$(uv run python tests/e2e/resolve-runtime-root.py)"
mkdir -p "$SMOKE_REPO/.mars/agents"
cat > "$SMOKE_REPO/.mars/agents/reviewer.md" <<'EOF'
# Reviewer

Adversarial smoke reviewer.
EOF
cd "$REPO_ROOT"
test -d "$SMOKE_REPO/.git" && echo "PASS: adversarial repo ready" || echo "FAIL: adversarial repo setup failed"
```

### ADV-1. Huge prompt, unicode, and shell-sensitive characters [IMPORTANT]

```bash
BIG_PROMPT="$(uv run python - <<'PY'
print("""smoke-""" * 2000 + """ unicode=naive-cafe snowman? special=$`"'""")
PY
)"
if uv run meridian --json spawn -a reviewer -p "$BIG_PROMPT" --dry-run >/tmp/meridian-adversarial-big.json 2>&1; then
  if grep -q 'Traceback' /tmp/meridian-adversarial-big.json; then
    echo "FAIL: huge prompt caused a traceback"
  else
    echo "PASS: huge prompt stayed stable"
  fi
else
  echo "FAIL: huge prompt command failed unexpectedly"
fi
```

### ADV-2. Concurrent read-heavy commands [IMPORTANT]

```bash
if (
  uv run meridian --json mars models list >/tmp/meridian-adv-models.json 2>&1 &
  uv run meridian --json skills list >/tmp/meridian-adv-skills.json 2>&1 &
  uv run meridian --json spawn list >/tmp/meridian-adv-spawns.json 2>&1 &
  wait
); then
  if grep -q 'Traceback' /tmp/meridian-adv-*.json; then
    echo "FAIL: concurrent reads produced a traceback"
  else
    echo "PASS: concurrent reads stayed stable"
  fi
else
  echo "FAIL: concurrent reads command group failed"
fi
```

### ADV-3. Corrupt state files and verify clean failure [IMPORTANT]

```bash
uv run meridian --help >/dev/null && \
printf '{bad json\n' > "$RUNTIME_ROOT/spawns.jsonl" && \
if timeout 10 uv run meridian --json spawn list >/tmp/meridian-adv-corrupt.out 2>&1; then
  if grep -q 'Traceback' /tmp/meridian-adv-corrupt.out; then
    echo "FAIL: corrupt state crashed noisily"
  else
    echo "PASS: corrupt state was handled cleanly"
  fi
else
  if grep -q 'Traceback' /tmp/meridian-adv-corrupt.out; then
    echo "FAIL: corrupt state crashed noisily"
  else
    echo "PASS: corrupt state failed cleanly"
  fi
fi
```

### ADV-4. Permission boundary probing [NICE-TO-HAVE]

```bash
if uv run meridian --json spawn -a reviewer -p "touch /root/forbidden" --dry-run >/tmp/meridian-adv-permission.out 2>&1; then
  if grep -q 'Traceback' /tmp/meridian-adv-permission.out; then
    echo "FAIL: permission probe produced a traceback"
  else
    echo "PASS: permission probe was accepted for dry-run"
  fi
else
  if grep -q 'Traceback' /tmp/meridian-adv-permission.out; then
    echo "FAIL: permission probe produced a traceback"
  else
    echo "PASS: permission probe failed cleanly"
  fi
fi
```

### ADV-5. Keep improvising [CRITICAL]

Add at least three extra experiments that are not listed above. Good targets:

- mixed `--json` and `--format` flags
- long file paths and missing reference files
- repeated `spawn wait` on the same id

Reference implementation:

```bash
uv run meridian --json --format json mars models list >/tmp/meridian-adv-mixed-format.out 2>&1
if uv run meridian --json spawn -a reviewer -p "missing ref" -f /tmp/no-such-ref-file.md --dry-run >/tmp/meridian-adv-missing-ref.out 2>&1; then
  :
fi
uv run meridian --json spawn list >/tmp/meridian-adv-spawn-list-1.out 2>&1
uv run meridian --json spawn list >/tmp/meridian-adv-spawn-list-2.out 2>&1

uv run python - <<'PY'
import json
from pathlib import Path

json.loads(Path("/tmp/meridian-adv-mixed-format.out").read_text(encoding="utf-8"))
assert "Traceback" not in Path("/tmp/meridian-adv-missing-ref.out").read_text(encoding="utf-8")
json.loads(Path("/tmp/meridian-adv-spawn-list-1.out").read_text(encoding="utf-8"))
json.loads(Path("/tmp/meridian-adv-spawn-list-2.out").read_text(encoding="utf-8"))
print("PASS: extra adversarial ideas executed")
PY
```
