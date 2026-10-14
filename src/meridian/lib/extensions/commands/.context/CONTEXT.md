# extensions/commands — Context

Command-specific contracts. The parent `.context/` covers the dispatcher
pipeline, handler signatures, and surface rules.

## `meridian.mermaid.check` (`mermaid.py`)

Runs in process and needs no runtime state.

Handler error codes:

- `node_not_found` — Node.js is absent from `PATH`
- `bundle_not_found` — the bundled Mermaid validator is missing
- `not_found` — the target path does not exist
- `args_invalid` — the provided path cannot be resolved

A path with zero Mermaid blocks is successful and returns
`ExtensionJSONResult(total_blocks=0)`. Only path resolution and tool availability
failures produce `ExtensionErrorResult`.

The spec has no `cli_group` or `cli_name`: `meridian ext run
meridian.mermaid.check` uses extension dispatch, while the direct `meridian
mermaid check` command keeps its existing CLI handler.

## Related

- [`../AGENTS.md`](../AGENTS.md) — file list and registration instructions
- [`../.context/CONTEXT.md`](../.context/CONTEXT.md) — dispatcher pipeline and spec contracts
