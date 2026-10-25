# Models

A profile sets the preferred `model`, its declared backups, and per-model settings in
`model-policies`. Callers can override the model with `-m`, and an explicit model never
falls back. Before choosing or changing a model, check the live catalog with
`meridian mars models list` (`--live` adds whether each row can run here) and any
model-specific prompting guidance with `meridian mars models prompting @AGENT` or an
alias. Guidance exists only where an alias declares it, so an empty answer means none
was written, not that the model needs none.

## Backups

Fallbacks are decided per agent: a model can be an automatic backup in one agent and
explicit-only in another. When you add a model to an agent's policies, mark it
`no-fallback: true` unless the human decided it belongs among that agent's backups.

Concrete `alias` and `model` entries in `model-policies` are the backups, in
declaration order; there is no separate fallback list. Keep the list short and add an
`override` only where a model needs different settings.

```yaml
model: opus
model-policies:
  - match: {alias: opus}
    no-fallback: true
    override: {effort: high}
  - match: {alias: sonnet}
  - match: {alias: gpt}
    override: {effort: high}
```

Opus is the primary here. Its `no-fallback` means only "never use this entry as a
backup"; it doesn't stop fallback to Sonnet or GPT, and it still supplies settings when
Opus is chosen. Fallback starts only when the primary's harness can't run it here.

- An entry with an empty or omitted `override` still declares a backup; keep such entries
  when editing a profile.
- `model-glob` rules supply settings but never become backups.
- The first rule that matches the chosen model supplies its settings.
- Changing the primary, also through an overlay, keeps the profile's backups. Rules
  from an overlay or from `mars.toml` settings supply settings, not more backups.

## Which Harnesses Can Run It

`targets` in `mars.toml` also decides which harnesses a launch may use.
`targets = [".claude"]` allows only Claude: a Pi or Codex model fails with "fallback
candidates exhausted ... on linked harnesses" even when that harness is installed and
logged in. Link it (`meridian mars link .pi`) to allow it; `--harness` can't widen the
list. With no `targets`, routing picks among installed harnesses. A model family is not a harness: an Anthropic
model may still run through an enabled OpenCode route.

The harness decides what the profile's permissions are worth (`resources/agents.md`). A
child that a sandboxed lead spawns on OpenCode or Pi is refused, because those harnesses
enforce no sandbox.

Fallback chooses a route before launch. It does not promise credits, and it does not
switch models after a runtime credit error.

## Changing an Installed Agent's Model

Set `model` in an `[agents.NAME]` overlay in `mars.toml`, or in `mars.local.toml` for
this machine only, rather than copying the profile. `[models.ALIAS]` defines or pins an
alias and can carry its prompting guidance; `meridian mars models aliases` lists the
aliases this project resolves. Confirm the result with the dry run, which prints the
chosen model and harness.
