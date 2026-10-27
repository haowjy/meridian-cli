# lib/markdown/

Shared markdown parsing for `lib/kg/` and `lib/mermaid/`: `extract_file` and `extract_text`
make one `markdown-it-py` pass for headings, fenced blocks, links, and wikilinks.

- **`extract_file` never raises**: an unreadable file returns an `ExtractedDocument` with
  `error` set and every other field empty. Check `doc.error` first, or empty fields read as
  a clean result.
- **Don't import `markdown_it` elsewhere**; route through these functions (the parser is a
  module-level singleton).
- **Wikilinks always have `resolved=None`**; callers define their semantics.
- **Anchors are stripped before path resolution**: `ExtractedLink.target` keeps the original,
  `.resolved` is the anchor-free path.
- **Line numbers are relative to the post-frontmatter body**; add the frontmatter line count
  for source lines.
- `extract_text`'s `path` need not exist; it only anchors relative links.

[.context/CONTEXT.md](.context/CONTEXT.md)
