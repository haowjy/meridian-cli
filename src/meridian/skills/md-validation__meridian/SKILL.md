---
name: md-validation__meridian
description: When checking Markdown links, a document tree's link graph, or Mermaid diagrams before committing them.
---

# Markdown Validation

Run `meridian kg check PATH` after writing, moving, or linking Markdown pages, and
`meridian mermaid check PATH` after editing a diagram. Fix every reported issue and rerun
until the output is clean. Both exit 0 on warnings; `--strict` makes warnings fail, which
is the right setting for a gate. `kg check` also catches leftover merge-conflict markers.

`meridian kg graph PATH` prints the link tree from each page that links out but has no
inbound links, `--depth` hops deep (default 3). A page with no links in or out does not
appear at all, so compare the tree with the file list. Read it before restructuring a
tree, and after a move or merge to confirm every page is still reached from where readers
enter. `--format json` adds link clusters and missing backlinks.

`kg check` and `kg graph` take a context name as PATH: `kb`, `strategy`, or `work`
resolve to this project's knowledge base, strategy, and active work directories. Use the
name instead of an absolute path that differs between machines. `mermaid check` does not
resolve them; pass the directory.

`meridian mermaid` prints the active parser. With `python heuristics` instead of the
`@mermaid-js/parser` node parser, a clean result catches less: say so when you report a
diagram as valid.

## Exclusions

Each validator reads a gitignore-style file at the root of the directory it scans:
`.kgignore` for `kg`, `.mermaidignore` for `mermaid`. Nothing is excluded by name
otherwise, apart from tool and build directories (`.git`, `node_modules`, `dist`,
`build`, virtualenvs and caches) for `kg`. An `archive/` directory, for example, is
validated until `.kgignore` lists it. Exclude generated, vendored, or deliberately
unsupported material; never use an ignore file to hide a broken page that belongs to the
tree.

For one run, `--exclude GLOB` (repeatable) does the same without a file.

## Mermaid Style Warnings

Style warnings flag diagrams that parse but probably don't mean what they say. Fix the
source when a warning marks a real ambiguity; suppress it only when the construct is
deliberate. Categories, what each catches, and the suppression comments:
`resources/mermaid-warnings.md`.
