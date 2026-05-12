# mermaid/style/

Style (non-syntax) checks for Mermaid diagrams. Produces `StyleWarning` items
about structural patterns that are technically valid but problematic. Separate
from the syntax validator in `../validator.py` — style warnings do not appear in
`BlockResult` and do not affect diagram validity.

## Mental Model

Two phases controlled by whether syntax validation has run:

**Pre-parse checks** — run on every target, valid or invalid, without needing diagram
type. When a block has a syntax error, pre-parse warnings for that block are silently
dropped (redundant alongside syntax errors).

**Post-parse checks** — run only on syntactically valid targets, may use `diagram_type`
to avoid false positives. For example, fill-no-color only applies where fill directives
are meaningful.

A check is pre-parse when the issue is detectable from raw text. A check is post-parse
when diagram type is needed to avoid false positives.

## Key Rules

**Line numbers must go through `content_line_to_file_line` before emitting a
`StyleWarning`.** `iter_diagram_lines()` yields content-relative line numbers.
Without the mapping, warnings point to wrong lines in the file.

**Suppressed warnings are collected, not discarded.** `SuppressionSet.is_suppressed()`
returns `(bool, source_string)`. Suppressed warnings go to `suppressed_warnings`
(not `active_warnings`) so callers can report what was suppressed.

**To add a check:**
1. Create a module with a `WarningCategory` constant and a `CheckFn` implementation.
2. Append `(CATEGORY, check_fn)` to `_PRE_PARSE` or `_POST_PARSE` in `__init__.py`.
3. `get_all_categories()` picks it up automatically — no registration elsewhere.

**`WarningCategory.diagram_types` filters by diagram type.** If set, the check only
runs when the diagram type is in the frozenset. `None` means any diagram type.
`_should_skip_category()` in `__init__.py` applies this filter before calling the check.

## Suppression Syntax

```
%% mermaid-check-ignore                   — suppress all warnings for the block
%% mermaid-check-ignore <category>        — suppress one category for the block
%% mermaid-check-ignore-next-line         — suppress all warnings on the next line
%% mermaid-check-ignore-next-line <cat>   — suppress one category on the next line
```

## Entry Points

- `types.py` — `StyleWarning`, `WarningCategory`, `StyleCheckOptions`, `CheckResult`
- `__init__.py` — `run_style_checks()`, `get_all_categories()`
- `suppression.py` — `parse_suppressions()`, `SuppressionSet`
- `line_map.py` — `content_line_to_file_line()`
- `preprocess.py` — `iter_diagram_lines()`

Existing checks: `bare_end.py`, `fill_no_color.py`, `ox_edge.py`.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — pre/post split rationale, line number
mapping mechanics, suppression scope, adding a check step-by-step

## Related

- `../AGENTS.md` — parent mermaid module: how style/ fits into the validation pipeline
- `../.context/CONTEXT.md` — parent mermaid contracts; style/ is a separate concern
  from syntax validation
