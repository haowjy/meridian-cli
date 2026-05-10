# lib/mermaid — Mermaid Diagram Validation

Validates Mermaid diagram blocks in markdown files, `.mmd` files, and
`.mermaid` files. Backs `meridian mermaid check`. Has two validation tiers:
Python heuristic (default, offline) and JS strict (optional, requires Node.js).

## Entry Points

- `validate_path(path, opts?) → MermaidValidationResult` — validate a file or directory
- `detect_tier() → ValidationTier` — returns `"js"` if Node.js + bundle available, else `"python"`
- `ScanOptions` — exclude patterns and depth limit for directory scans
- Result types: `MermaidValidationResult`, `BlockResult` in `validator.py`

## Submodule

- `style/` — style warning checks (separate from syntax validation); `StyleCheckOptions`, `StyleWarning`

## Dependencies

- Uses `scanner.py` (internal) to collect `DiagramTarget` objects from files
- Scanner delegates file parsing to `lib/markdown/extract.extract_file`, then filters for `language == "mermaid"` fenced blocks

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — tier contracts, validation pipeline, ignore patterns
