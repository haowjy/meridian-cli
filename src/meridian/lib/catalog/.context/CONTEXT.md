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
