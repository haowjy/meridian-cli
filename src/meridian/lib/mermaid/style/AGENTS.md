# mermaid/style/

Style (non-syntax) checks: `StyleWarning`s for valid-but-problematic structure. Separate
from `../validator.py`; warnings never affect diagram validity.

- **Pre-parse checks** run on every target from raw text; for a block with a syntax error
  its pre-parse warnings are dropped. **Post-parse checks** run only on syntactically valid
  targets and may use `diagram_type` to avoid false positives. A check is post-parse when
  it needs the diagram type.
- **Map line numbers through `content_line_to_file_line` before emitting a `StyleWarning`**;
  `iter_diagram_lines()` yields content-relative lines.
- **Suppressed warnings are collected, not discarded**: they go to `suppressed_warnings` so
  callers can report them (`SuppressionSet.is_suppressed()` returns `(bool, source)`).
  Directives: `%% mermaid-check-ignore[-next-line] [<category>]`.
- **Adding a check**: a module with a `WarningCategory` constant and a `CheckFn`, appended
  to `_PRE_PARSE` or `_POST_PARSE` in `__init__.py`; `get_all_categories()` picks it up.
  `WarningCategory.diagram_types` (a frozenset, `None` = any) is applied by
  `_should_skip_category()`.

[.context/CONTEXT.md](.context/CONTEXT.md) · [../AGENTS.md](../AGENTS.md)
