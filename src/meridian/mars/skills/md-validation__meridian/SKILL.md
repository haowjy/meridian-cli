---
name: md-validation__meridian
description: When checking Markdown links, a document tree's link graph, or Mermaid diagrams before committing them.
---

# Markdown Validation

Run `meridian kg check PATH` after writing, moving, or linking Markdown pages, and
`meridian mermaid check PATH` after editing a diagram. Both exit 0 on warnings;
`--strict` makes warnings fail, which is the right setting for a gate. `kg check` also
catches leftover merge-conflict markers. Fix what they report and rerun until the output
is clean: no errors and no warnings left. A Mermaid style warning suppressed in the
diagram with its reason beside it counts as clean. A category disabled for the whole run
with `--disable` does not, since it hides every other instance too.

`meridian kg graph PATH` prints the link tree from each page that links out but has no
inbound links. A page with no links at all does not appear. Read it before restructuring
a tree, and after a move or merge look for pages that lost their inbound links: they now
show up as new roots, or not at all.

`kg check` and `kg graph` take a context name as PATH: `kb`, `strategy`, or `work`
resolve to this project's knowledge base, strategy, and active work directories. Use the
name instead of an absolute path that differs between machines. `mermaid check` does not
resolve them; pass the directory.

`meridian mermaid` prints the active parser. With `python heuristics` instead of the
`@mermaid-js/parser` node parser, a clean result catches less: say so when you report a
diagram as valid.

## What a Scan Skips

Both validators skip tool and build directories (`.git`, `node_modules`, `dist`,
`build`, virtualenvs, caches) without saying so, and a scan that skipped everything still
reports success. To check pages or diagrams kept inside one, pass that directory or file
as PATH.

Each also reads a gitignore-style file at the root of the directory it scans: `.kgignore`
for `kg` (for a single file, the file's directory), `.mermaidignore` for `mermaid` (read
only when PATH is a directory). Nothing is excluded by name otherwise: an `archive/`
directory is validated until `.kgignore` lists it. Exclude generated, vendored, or
deliberately unsupported material; never use an ignore file to hide a broken page that
belongs to the tree.

## Mermaid Style Warnings

Style warnings flag diagrams that parse but probably don't mean what they say. Fix the
source when a warning marks a real ambiguity; suppress it only when the construct is
deliberate. Categories, what each catches, and the suppression comments:
`resources/mermaid-warnings.md`.
