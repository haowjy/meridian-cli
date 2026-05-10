# lib/markdown — Markdown Extraction

Shared parsing layer (wraps `markdown-it-py`). Extracts headings, fenced code
blocks, links, and wikilinks from markdown files. Not user-facing — internal
shared library for `lib/kg/` and `lib/mermaid/`.

## Entry Points

- `extract_file(path: Path) → ExtractedDocument` — reads and parses one file
- `extract_text(text: str, path: Path) → ExtractedDocument` — parses from string (useful for tests)
- Types: `ExtractedDocument`, `ExtractedHeading`, `ExtractedLink`, `FencedBlock` in `types.py`

## Consumers

- `lib/kg/` — uses `extract_file` to build graph nodes and link edges
- `lib/mermaid/` — uses `scanner.py` which collects fenced blocks with `language == "mermaid"`

## Depth

→ [.context/CONTEXT.md](.context/CONTEXT.md) — extraction contracts and known limitations
