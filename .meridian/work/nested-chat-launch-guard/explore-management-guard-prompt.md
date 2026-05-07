Find why `meridian chat` management subcommands (`ls/show/log/close`) are still guarded as root-only/nested-disallowed.

Please inspect the current code and tests/history enough to answer:
- Where is the guard applied?
- Is there an explicit rationale in code/tests/docs/commit history?
- What risks does it seem intended to prevent?
- Would removing it be safe now that chat launch is allowed nested, or would it need targeted protections?

Do not modify files. Return a concise explanation with file/test references.
