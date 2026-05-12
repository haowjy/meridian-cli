# lib/mermaid/

Validates Mermaid diagram blocks in markdown files and standalone `.mmd`/`.mermaid`
files. Backs `meridian mermaid check`. Has two validation tiers: Python heuristic
(default, offline) and JS strict (requires Node.js + bundle).

## Mental Model

`validate_path` is the only public API callers should use. It:
1. Collects diagram targets (fenced `mermaid` blocks + standalone files) via `scanner.py`
2. Detects the available tier (`detect_tier()`)
3. Validates each block with the appropriate backend
4. Returns `MermaidValidationResult` aggregating all `BlockResult` objects

The two tiers are not equivalent:

| Tier | How | What it catches | Notes |
|---|---|---|---|
| Python (default) | Heuristic — type detection + structural checks | Missing `end`, unclosed directives | Fast, offline, misses syntax errors within valid structure |
| JS strict | Spawns `node mermaid-validator.bundle.js` | Full Mermaid parse errors | Authoritative; 10s timeout per block |

Tier selection is automatic — JS activates when Node.js and the bundle are available.
No config needed.

## Key Rules

**Use `validate_path`, not `_validate_block_python` or `_validate_block_js` directly.**
The public API handles file collection, tier selection, and result aggregation. Calling
internal functions bypasses `.mermaidignore` and scan root normalization.

**`validate_path` raises `FileNotFoundError` if the path doesn't exist.** The CLI
translates this to exit code 2. Check path existence before calling, or catch
`FileNotFoundError`.

**`BlockResult.file` is relative to the scan root.** When the target is a file,
the scan root is that file's parent directory. When it's a directory, the scan root
is the directory itself. Absolute paths are not emitted.

**Style warnings are separate from syntax validation.** `MermaidValidationResult`
contains only syntax results. Style issues (bare `end`, fill-no-color patterns,
ox-edge connectors) come from `style/` and produce a separate `style.CheckResult`.
Don't look for style warnings in `MermaidValidationResult.block_results`.

## Submodule: `style/`

Style checks run on top of syntax validation results. `run_style_checks(targets,
validation_results, options)` returns `(active_warnings, suppressed_warnings)`.
Pass the same `targets` and `validation_results` from `validate_path` — the style
module needs them both.

## Entry Points

- `validator.py` — `validate_path()`, `detect_tier()`, `MermaidValidationResult`, `BlockResult`
- `style/` — `run_style_checks()`, `StyleWarning`, `StyleCheckOptions`
- `scanner.py` — `collect_targets()`, `DiagramTarget` (internal; usable in tests)

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — tier contracts, Python heuristic
algorithm, JS bundle path, per-block timeout, `.mermaidignore` application

→ [style/.context/CONTEXT.md](style/.context/CONTEXT.md) — pre/post-parse split,
suppression directives, line number mapping, adding a new check

## Related

- `../markdown/` — `extract_file()` is used by `scanner.py` to find fenced blocks
