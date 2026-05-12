# lib/markdown/

Centralized markdown parsing layer. Two functions: `extract_file` and `extract_text`.
Internal shared library — `lib/kg/` and `lib/mermaid/` both consume it. Not
user-facing; no CLI commands point here directly.

## Mental Model

One pass over the `markdown-it-py` token stream extracts headings, fenced code
blocks, links, and wikilinks from a file. The result is an `ExtractedDocument`
that callers filter for what they need. `lib/mermaid/scanner.py` filters
`fenced_blocks` for `language == "mermaid"`; `lib/kg/graph.py` uses `references`
to build graph edges.

## Key Rules

**`extract_file` never raises.** Unreadable files return an `ExtractedDocument` with
`error` set and all other fields empty. Check `doc.error` before using
`doc.references` or `doc.fenced_blocks` — consuming empty fields from an error
document produces silent incorrect results.

**Do not import `markdown_it` directly in other modules.** Route through `extract_file`
or `extract_text`. The parser is a module-level singleton; re-instantiating it
elsewhere wastes memory and fragments the parse configuration.

**Wikilinks always have `resolved=None`.** The extraction layer has no filesystem
context to resolve `[[target]]` syntax. Semantics are caller-defined. `lib/kg/`
treats them as unresolvable.

**Link anchors are stripped before path resolution.** `page.md#section` resolves as
`page.md`. The full original target (including anchor) is in `ExtractedLink.target`;
the anchor-free `Path` is in `ExtractedLink.resolved`.

**Line numbers in headings and links are relative to the post-frontmatter body.**
YAML frontmatter is stripped before the token walk. If you need source line numbers
that account for frontmatter, add the frontmatter line count back.

**`extract_text` is the testing entry point.** The `path` argument doesn't need to
exist — it's used only for relative link resolution context.

## Entry Points

- `extract.py` — `extract_file(path)`, `extract_text(text, path)`
- `types.py` — `ExtractedDocument`, `ExtractedHeading`, `ExtractedLink`, `FencedBlock`

## Related

- `../kg/` — primary consumer: builds graph nodes and edges from `extract_file`
- `../mermaid/scanner.py` — filters `fenced_blocks` for Mermaid diagram targets
