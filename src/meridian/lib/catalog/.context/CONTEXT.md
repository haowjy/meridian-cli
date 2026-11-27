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
can be loaded. Missing or non-boolean values default to true for compatibility.
See the [agent catalog](../../launch/.context/CONTEXT.md).

Meridian has no agent lookup of its own: Mars resolves every name, and Meridian parses
the file Mars chose (`parse_agent_profile` on the bundle's `agent_source_path`, or
`mars agents show` through `ops/mars.py` where no bundle is built). A second lookup could
pick a different file than the one that runs. Mars reads profile skills when building the
launch bundle for the same reason. Every by-name call to Mars sends `mars_content_args`
(`mars_roots.py`): the user root as `--fallback-root`, then this build's bundle directory
as `--provided-root`.

Built-in profiles (`meridian/mars/agents/`, today `inbox-router`) come from that provided
layer, which answers a name only when no installed agent claims it; a name Mars renamed on
a collision stays its `agent_renamed` error. They are read in place, never installed
through `mars provide`: an installed copy could go missing whenever another sync held the
lock or another Meridian version provided a different bundle.
