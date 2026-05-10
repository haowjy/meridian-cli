# lib/kg — Contracts and Architecture

## Architecture

`build_analysis` is the core function. It:
1. Walks the root directory (or targeted path), collecting `.md` files
2. Calls `extract_file` (from `lib/markdown/`) for each, building `GraphNode` entries
3. Converts each `ExtractedLink` into a `GraphEdge` — resolved if the target file exists
4. Computes in-degree, orphans (retained for compatibility but currently empty), missing backlinks, and connected clusters
5. Returns `AnalysisResult`

`build_check` is a thin wrapper over `build_analysis` that disables backlink and cluster computation, then adds content findings (flag blocks, conflict markers) by scanning file text line by line.

```
build_check(root)
  └── build_analysis(root, include_backlinks=False, include_clusters=False)
        └── extract_file(path)   [lib/markdown/]
              └── returns ExtractedDocument
        └── _make_edge(src, ref, nodes)  → GraphEdge
  └── _scan_file_findings(path)  → CheckFinding list
        flag blocks: "> [!FLAG]" pattern (outside fenced blocks)
        conflict markers: "<<<<<<<"  "======="  ">>>>>>>"
```

## Contracts

**Callers must pass a resolved root.** `build_analysis` calls `root.resolve()` internally, but passing an unresolved path with symlinks can cause relative-path display differences.

**`CheckResult.has_errors` drives the exit code** for `meridian kg check`. Broken links are emitted as `warning` severity by default; `--strict` promotes them to `error`. Flag blocks and conflict markers follow the same `warning`/`error` logic. Check `has_errors`, not `findings` length.

**Wikilinks are never resolved.** `GraphEdge.dst` is a raw string (not `Path`) for wikilinks, and `resolved=False`. The analysis skips wikilinks from broken-link counts — they're structural references, not filesystem links.

**External links are always resolved.** Any target starting with `http://`, `https://`, `mailto:`, or `#` sets `resolved=True` without checking. These are excluded from `broken_links`.

**`.kgignore` at the scan root is applied automatically.** Gitignore-style patterns via `lib/ignores.py`. Per-call `--exclude` patterns stack on top; they do not replace `.kgignore`.

## Patterns

Use `build_check` for validation gates (broken links + content issues). Use `build_analysis` when you need the graph structure (topology, clusters, backlinks). Don't call `build_analysis` and re-run `build_check` to save time — `build_check` already calls `build_analysis` internally.

`targeted_path` scopes the scan to a file or subdirectory but still resolves links against the full scan root. Useful for incremental checks on large KBs.

## Related KB

→ [KB: codebase/tools.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/codebase/tools.md)

## Lateral Links

→ [../../markdown/.context/CONTEXT.md](../../markdown/.context/CONTEXT.md) — extraction layer this module consumes
