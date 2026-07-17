# lib/mermaid — Contracts and Tier Architecture

## Architecture

```
validate_path(path, opts)
  └── collect_targets(path, root, ...)      [scanner.py]
        finds fenced mermaid blocks + standalone .mmd/.mermaid files
  └── detect_tier()
        "js"  if node on PATH and bundle exists
        "python"  otherwise (default)
  └── _validate_block_python(target)   OR   _validate_block_js(target)
        → BlockResult per diagram
  └── aggregates into MermaidValidationResult
```

**Python tier** (`_validate_block_python`): two-phase heuristic — detect diagram type from the first non-comment line, then check for unclosed `%%{...}%%` directives and missing `end` keywords for block openers (`loop`, `alt`, `opt`, `par`, `rect`, `subgraph`, `if`). Fast, offline, catches structural errors. Does not catch syntax errors within valid structure.

**JS tier** (`_validate_block_js`): spawns `node mermaid-validator.bundle.js <tmpfile>`, parses JSON result. Authoritative. Returns `diagramType` on success. The bundle lives at `validator.py:BUNDLE_PATH` (sibling to `validator.py`). Tier upgrade is automatic — no config needed.

## Contracts

**`validate_path` raises `FileNotFoundError` if the path doesn't exist.** The CLI translates this to exit code 2. Callers should check path existence before calling, or catch `FileNotFoundError`.

**JS tier validation has a 10-second per-block timeout.** `TIMEOUT_SECS = 10` in `validator.py`. Timeout produces `BlockResult(valid=False, error="validation timed out")`. The Python tier has no timeout.

**`detect_tier` is side-effect-free and cheap.** It's called once per `validate_path` invocation, not cached. Safe to call at any time.

**`.mermaidignore` at the scan root is applied by `scanner.py`.** Uses `lib/ignores.py` (same pattern as `.kgignore`). Per-call `ScanOptions.exclude` stacks on top.

**`BlockResult.file` is relative to the scan root.** Absolute paths are not emitted. The scan root is the directory of the target path when `path.is_file()`, or `path` itself when it's a directory.

## Patterns

Use `validate_path` — don't call `_validate_block_python` or `_validate_block_js` directly. The public API picks the right tier and handles file collection. If you need to test a specific tier, call `_validate_block_python` with a `DiagramTarget` constructed manually (acceptable in tests, not in application code).

The `style/` submodule is a separate concern from syntax validation. It checks stylistic issues (bare `end`, fill-no-color patterns, ox-edges). `StyleCheckOptions` controls what's flagged. Style warnings do not appear in `MermaidValidationResult` — they come from a separate `style.CheckResult`.

## Related KB

→ KB: `$MERIDIAN_CONTEXT_KB_DIR/codebase/tools.md` (see `meridian context kb`)
