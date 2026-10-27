# lib/mermaid/

Validates Mermaid blocks in markdown and standalone `.mmd`/`.mermaid` files; backs
`meridian mermaid check`. Two tiers, chosen automatically: Python heuristic (offline;
misses syntax errors inside valid structure) and JS strict (`node` plus the bundle;
authoritative, 10s per block).

- **Call `validate_path`**, not `_validate_block_python`/`_validate_block_js`: only it
  applies `.mermaidignore`, scan-root normalization, and tier selection. It raises
  `FileNotFoundError` for a missing path (the CLI maps it to exit 2).
- **`BlockResult.file` is relative to the scan root** (a file target's parent, or the
  directory itself); no absolute paths.
- **Style warnings are separate from syntax results.** They come from
  `style.run_style_checks(targets, validation_results, options)`, given the same `targets`
  and `validation_results` as `validate_path` produced; they never appear in
  `MermaidValidationResult.block_results`.

[.context/CONTEXT.md](.context/CONTEXT.md) — tiers, heuristic algorithm, JS bundle ·
[style/AGENTS.md](style/AGENTS.md)
