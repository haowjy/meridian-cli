# Models cache refresh (network opt-in)

This guide probes the real Mars catalog/cache boundary. It is **never** part of
the default smoke or automated gate: `mars models refresh` performs a network
fetch, and a subsequent live spawn may be billable. No mtime-only assertion is
used; inspect the cache JSON's `fetched_at` and model payload.

## Disposable prerequisites

Run from a fresh shell and provide real, explicit fixtures. Do not run these
commands in an existing project:

```bash
. tests/smoke/scripts/setup.sh --git
uv run meridian mars init --root "$SCRATCH"

# Optional live-spawn case only: a local/known package and profile in SCRATCH.
# There are no placeholder aliases; stop if these are not set deliberately.
export SMOKE_MARS_PACKAGE=/absolute/path/to/a/disposable/mars-package
export SMOKE_MARS_AGENT=known-agent-in-that-package
```

`SMOKE_MARS_PACKAGE` must be a package you own or have permission to read, and
`SMOKE_MARS_AGENT` must be installed by the add below. A network fetch is still
required for the catalog refresh; approve it explicitly.

## Cold and stale cache refresh

```bash
rm -f "$SCRATCH/.mars/models-cache.json"
uv run meridian mars models refresh --root "$SCRATCH" --json >"$SCRATCH/refresh.json"
uv run python - "$SCRATCH/.mars/models-cache.json" <<'PY'
import json, sys
cache = json.load(open(sys.argv[1], encoding="utf-8"))
assert cache.get("fetched_at"), cache
assert isinstance(cache.get("models"), list), cache
PY

uv run python - "$SCRATCH/.mars/models-cache.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
data["fetched_at"] = 1
data["models"] = data.get("models", [])
json.dump(data, open(path, "w", encoding="utf-8"))
PY
before=$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["fetched_at"])' "$SCRATCH/.mars/models-cache.json")
uv run meridian mars models refresh --root "$SCRATCH" --json >/dev/null
after=$(uv run python -c 'import json,sys; print(json.load(open(sys.argv[1]))["fetched_at"])' "$SCRATCH/.mars/models-cache.json")
[[ "$after" != "$before" ]] || { echo "cache timestamp did not refresh" >&2; exit 1; }
```

The content/timestamp checks establish a completed refresh; filesystem mtime is
not evidence of how many requests occurred.

## Offline failure is bounded

```bash
rm -f "$SCRATCH/.mars/models-cache.json"
if MARS_OFFLINE=1 uv run meridian mars models refresh --root "$SCRATCH" --json; then
  echo "expected offline refresh to fail" >&2
  exit 1
fi
```

Check the error names offline mode and the refresh operation, and that the
command returns promptly rather than hanging.

## Optional local-package spawn

Only after the refresh checks, and only with explicit credentials/model approval:

```bash
uv run meridian mars add --root "$SCRATCH" "$SMOKE_MARS_PACKAGE"
uv run meridian mars agents list --root "$SCRATCH" --json
uv run meridian --directory "$SCRATCH" spawn -a "$SMOKE_MARS_AGENT" \
  --dry-run -p 'cache probe (no harness launch)'
```

`--dry-run` avoids launching the harness, but catalog resolution can still use
network/cache state. Keep this case opt-in and clean the entire `SMOKE_ROOT`
with `smoke_cleanup` afterward.
