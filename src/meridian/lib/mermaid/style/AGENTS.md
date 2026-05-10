# mermaid/style/

Style (non-syntax) checks for Mermaid diagrams. Separate from the syntax
validator in `../validator.py` — produces `StyleWarning` items, not
`BlockResult` validity.

## Files

- `types.py` — public contracts: `StyleWarning`, `WarningCategory`, `StyleCheckOptions`, `CheckResult`
- `__init__.py` — `run_style_checks()`: orchestrates pre-parse and post-parse checks
- `suppression.py` — `parse_suppressions()`, `SuppressionSet`: `%% mermaid-check-ignore` directives
- `bare_end.py` — warns on bare lowercase `end` in flowcharts
- `fill_no_color.py` — warns on fill directives with no color
- `ox_edge.py` — warns on ox-edge connector syntax
- `line_map.py` — maps content-relative line numbers to file-absolute line numbers
- `preprocess.py` — `iter_diagram_lines()`: strips diagram header for check iteration

## Entry Points

`run_style_checks(targets, validation_results, options)` — returns
`(active_warnings, suppressed_warnings)`. Pass the same `targets` and
`validation_results` that the validator produced.

To add a new check, implement `CheckFn`, register it in the `_PRE_PARSE` or
`_POST_PARSE` list in `__init__.py`, and define a `WarningCategory` constant.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) for:
- Pre-parse vs post-parse check distinction and why it matters
- Suppression directive syntax and scope (block vs next-line)
- Diagram-type filtering in `WarningCategory.diagram_types`

## Related

- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent mermaid module contracts;
  style/ is a separate concern from syntax validation
