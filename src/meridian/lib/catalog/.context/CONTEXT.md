# Catalog context

## Mars roots

Mars resolves package content from the nearest `mars.toml`, or the user Mars
root when none exists. Project content can fall back to user packages when the
installed Mars supports fallback roots; other Mars configuration comes from the
primary root. Read-pure inspection may report an absent user root but must not
initialize it or launch Mars: bare-directory `doctor` depends on this distinction.

## Models

Meridian defines no builtin model aliases. Mars resolution fails loudly when
Mars cannot run, while alias inventory can fall back to `.mars/models-merged.json` under the primary Mars root. That
file contains pinned aliases only; auto-resolve aliases need Mars to expand them.
Inventory is static, not live routing availability. Mars subprocesses allow 60
seconds because a cold models.dev refresh can be slow.

Cache-only and normal alias lookups have separate cache keys so a dry run cannot
suppress a later refresh within the same operation. Missing results are cached
for the operation as well.

Mars can prefix-match a literal model ID to a different ID. The direct resolution
helper checks the catalog for an exact literal match before accepting that result;
the catalog proves identity, not a harness route. Static aliases and exact-ID
matches can legitimately lack a harness.

## Agent profiles

`model-invocable` controls default subagent visibility, not whether a profile
can be loaded. Keep catalog scanning neutral: explicit agent selection and other
consumers need every profile. Missing or non-boolean values default to true for
compatibility. See the [agent catalog](../../launch/.context/CONTEXT.md).

Mars reads profile skills when building the launch bundle. A separate Meridian
parse would diverge from the skills actually loaded.

Built-in profiles (`meridian/mars/agents/`, today `inbox-router`) are read from the
installed wheel only when the Mars lookup would otherwise end in "not found", never
installed into a Mars root. The collision-rename and user-packages errors run first, so a
user's own copy is never silently replaced by the built-in (the built-in directory is not
one more Mars root, and `_agent_search_dirs` must not list it). Never installed because an
installed copy could go missing whenever another sync held the lock or another Meridian
version provided a different bundle. A launch sends the resolved profile's path to Mars as
`--agent-file`, so Meridian's lookup and Mars's always read the same file; overlays stay
keyed by the name. `mars agents list` and `spawn subagents --all` list `mars.lock` items
and so don't show built-in profiles.
