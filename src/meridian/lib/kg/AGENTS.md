# lib/kg/

Document graph analysis for knowledge bases. Builds a link graph from markdown files
and identifies structural problems — broken links, conflict markers, flag blocks.
Backs `meridian kg check` and `meridian kg graph`.

## Mental Model

Two entry points with different cost/scope tradeoffs:

- **`build_check`** — validation gate. Calls `build_analysis` internally with
  backlinks and clusters disabled, then adds content findings (flag blocks, conflict
  markers). Use for CI gates and pre-commit checks.
- **`build_analysis`** — full graph: nodes, edges, clusters, backlinks. Use when
  you need topology or want to visualize the graph.

Don't call both for the same check — `build_check` already calls `build_analysis`
internally. Calling `build_analysis` and then `build_check` double-scans the same
files.

## Key Rules

**Check `has_errors`, not `findings` length.** `CheckResult.has_errors` is what drives
the exit code. Broken links emit as `warning` severity by default; `--strict` promotes
them to `error`. A non-empty `findings` list does not mean the gate fails.

**Wikilinks are never resolved.** `GraphEdge.dst` for wikilinks is a raw string (not
`Path`), and `resolved=False`. Wikilinks are excluded from broken-link counts.
Attempting to resolve them from outside this module will give wrong results — the
semantics are caller-defined.

**External links are always marked resolved.** Any target starting with `http://`,
`https://`, `mailto:`, or `#` is excluded from `broken_links` without filesystem checks.

**`.kgignore` at the scan root is applied automatically.** `--exclude` patterns stack
on top; they don't replace `.kgignore`. Pass a resolved `root` — `build_analysis`
calls `root.resolve()` internally, but symlinks in an unresolved path cause display
differences in relative paths.

**`targeted_path` scopes the scan but resolves links against the full root.** Useful
for incremental checks on large KBs without losing cross-KB link validation.

## Entry Points

- `graph.py` — `build_analysis()`, `build_check()`: the two public functions
- `types.py` — `AnalysisResult`, `CheckResult`, `CheckFinding`, `GraphNode`, `GraphEdge`
- `report.py` — human-readable formatting
- `serializer.py` — JSON serialization

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — full pipeline walkthrough, contracts
on wikilinks/external links, targeted_path semantics, content findings detection

## Related

- `../markdown/` — `extract_file()` is the file parsing layer this module consumes
- `../ignores.py` — `.kgignore` pattern matching
