# Catalog

Read Mars outputs rather than its config files: direct parsing misses local and
user layers. Do not infer a harness from a model name; routing comes from the
launch bundle. Keep catalog caches scoped to one launch so alias changes are
visible on later invocations.

[.context/CONTEXT.md](.context/CONTEXT.md) describes Mars integration.
