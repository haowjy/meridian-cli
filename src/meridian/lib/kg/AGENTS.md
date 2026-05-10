# lib/kg — Knowledge Graph Analysis

Document graph analysis for knowledge bases: broken links, connected clusters,
missing backlinks. Backs `meridian kg` commands.

## Entry Points

- `build_check(root, ...)` — broken-link gate used by `meridian kg check`
- `build_analysis(root, ...)` — full graph (nodes, edges, clusters, backlinks) used by `meridian kg graph`
- Result types: `AnalysisResult`, `CheckResult`, `CheckFinding` in `types.py`
- Formatting: `report.py`; JSON serialization: `serializer.py`

## Dependencies

- Depends on `../markdown/` for file parsing (`extract_file`).
- Uses `../ignores.py` for `.kgignore` pattern matching.

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — contracts, analysis pipeline, anti-patterns
