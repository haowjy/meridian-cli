# mermaid/style — Context

Style check mechanics. The parent `.context/` establishes that style warnings
are separate from syntax validation and controlled by `StyleCheckOptions` — this
file explains the pre/post split, suppression format, and how to add checks.

## Pre-Parse vs Post-Parse Split

`run_style_checks()` divides checks into two lists:

**Pre-parse** (`_PRE_PARSE`): Run on every target, valid or invalid. These checks
operate on raw diagram text without knowing the diagram type. When a block has a
syntax error, pre-parse warnings for that block are silently dropped — they would
be redundant and potentially misleading alongside syntax errors.

**Post-parse** (`_POST_PARSE`): Run only on targets that passed syntax validation.
These checks may use `diagram_type` from the validation result (e.g., `fill-no-color`
only applies where fill directives are meaningful).

A check is pre-parse when it can detect the issue from raw text before knowing the
diagram type. A check is post-parse when it needs a parsed diagram type to avoid
false positives.

## Line Number Mapping

Style warnings carry a `line` field relative to the file, not the diagram block.
`content_line_to_file_line(target, content_line)` in `line_map.py` handles the
conversion:
- Fenced blocks (`source == "fenced-block"`): offset by `target.start_line`
- Standalone `.mmd` files: line numbers are already file-absolute

`iter_diagram_lines()` in `preprocess.py` yields `(content_line, stripped_text)` pairs,
where `content_line` is relative to the diagram block. Pass through `content_line_to_file_line`
before emitting a `StyleWarning`.

## Suppression Directives

Suppression comments in Mermaid diagrams use `%%` comment syntax:

```
%% mermaid-check-ignore                   — suppress all warnings for the block
%% mermaid-check-ignore <category>        — suppress one category for the block
%% mermaid-check-ignore-next-line         — suppress all warnings on the next line
%% mermaid-check-ignore-next-line <cat>   — suppress one category on the next line
```

`parse_suppressions(content)` parses a `SuppressionSet` from block content.
`SuppressionSet.is_suppressed(content_line, category)` returns `(bool, source_string)`.
Suppressed warnings are collected into `suppressed_warnings` (not discarded) so
callers can report what was suppressed.

## WarningCategory and Diagram-Type Filtering

Each check registers a `WarningCategory` constant with:
- `id`: string key used in suppression directives and `disabled_categories`
- `diagram_types: frozenset[str] | None`: if set, the check only runs when the
  diagram type is in this set. `None` means any diagram type.
- `default: bool`: whether the check is on by default (informational; enforcement
  is currently via `disabled_categories` in `StyleCheckOptions`).

`_should_skip_category()` in `__init__.py` applies both the disabled-categories
filter and the diagram-type filter before calling a check function.

## Adding a Check

1. Create a module (e.g. `my_check.py`) with:
   - `MY_CHECK_CATEGORY = WarningCategory(id="my-check", ...)`
   - `def check_my_check(target: DiagramTarget, diagram_type: str | None) -> list[StyleWarning]:`
2. Register in `__init__.py`:
   - Pre-parse: append `(MY_CHECK_CATEGORY, check_my_check)` to `_PRE_PARSE`
   - Post-parse: append to `_POST_PARSE`
3. `get_all_categories()` picks it up automatically.

## Related

- [`../AGENTS.md`](../AGENTS.md) — file list and entry points
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — parent mermaid contracts
