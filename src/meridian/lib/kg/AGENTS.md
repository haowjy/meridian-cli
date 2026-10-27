# lib/kg/

Document graph analysis for knowledge bases; backs `meridian kg check` and `kg graph`.
Consumes `../markdown/` `extract_file()`.

- **`build_check`** is the validation gate: it calls `build_analysis` internally (backlinks
  and clusters off) then adds content findings (flag blocks, conflict markers). Don't call
  both for one check; that scans the files twice. `build_analysis` alone is for topology.
- **Check `CheckResult.has_errors`, not `findings` length.** It drives the exit code; broken
  links are `warning` unless `--strict`.
- **Wikilinks are never resolved**: `GraphEdge.dst` is a raw string, `resolved=False`, and
  they are excluded from broken-link counts. Don't resolve them outside this module.
- **External targets** (`http://`, `https://`, `mailto:`, `#`) are never filesystem-checked.
- **`.kgignore` at the scan root always applies**; `--exclude` stacks on top. Pass a
  resolved `root`.
- **`targeted_path` scopes the scan but resolves links against the full root.**

[.context/CONTEXT.md](.context/CONTEXT.md) — pipeline, contracts, content findings.
