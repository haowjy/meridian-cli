# lib/markdown — Contracts and Extraction Notes

## Contracts

**`extract_file` never raises.** If the file can't be read (permissions, missing), it returns an `ExtractedDocument` with `error` set and all other fields empty. Callers must check `doc.error` before using `doc.references` or `doc.fenced_blocks`.

**`extract_text` is for testing.** It takes raw text and a `Path` for link resolution context. The path doesn't need to exist — it's used only to resolve relative link targets.

**Wikilinks always have `resolved=None`.** The extraction layer has no filesystem context for wikilinks (`[[target]]` syntax). Resolution semantics are caller-defined; `lib/kg/` treats them as unresolvable references.

**Link targets strip anchors before resolution.** `page.md#section` resolves as `page.md`. The full original target (including anchor) is preserved in `ExtractedLink.target`; `ExtractedLink.resolved` holds the anchor-free `Path`.

**Frontmatter is stripped before token parsing.** YAML frontmatter between `---` delimiters is extracted into `ExtractedDocument.frontmatter` and removed before `markdown-it-py` processes the body. Line numbers in headings and links are relative to the post-frontmatter body — callers that need source line numbers should account for frontmatter line count.

## Architecture

The extraction pipeline processes three passes over the `markdown-it-py` token stream:
1. `_walk_headings` — `heading_open` tokens
2. `_walk_fences` — `fence` tokens (captures language tag and content)
3. `_walk_links` + `_walk_wikilinks` — `inline` children and `image` tokens; wikilinks via regex over `text` children

All three passes run in a single `extract_text` call. The shared `MarkdownIt()` instance is a module-level singleton — safe for concurrent reads, not for mutation.

## Patterns

Don't import `markdown_it` directly in other modules. Route through `extract_file` or `extract_text` so the parser is centralized. If you need only fenced blocks (as `lib/mermaid/scanner.py` does), filter `doc.fenced_blocks` by language rather than re-parsing.

## Related KB

→ [KB: codebase/tools.md](/home/jimyao/.meridian/git/meridian-flow-docs/kb/codebase/tools.md)

## Lateral Links

→ [../../kg/.context/CONTEXT.md](../../kg/.context/CONTEXT.md) — primary consumer
