## Why

Three explicitly-parked design decisions all touch the same surface — who owns environment variables and project identity: child-env recomposition vs consuming `final_env`, the `_MERIDIAN_*` internal/public prefix convention, and repo-local `.meridian/` vs `meridian.toml` identity. Blast radius spans prompt packages, hooks, pi_runtime, and mars-agents, so they deserve one coordinated pass, not three drive-bys.

## Goal

One written decision set: env-var namespace convention, single child-env composition point, and project identity in `meridian.toml` — then mechanical migration.

## Status

Shipped on `task/env-identity-ownership`. This file records the historical plan;
the implementation now has:

- **#361** — connections consume the complete bind-resolved child environment;
  no connection adapter recomposes it from `os.environ`.
- **#336** — a tiered environment registry distinguishes stable `MERIDIAN_*`
  contracts from internal `_MERIDIAN_*` transport and drives child propagation.
- **#341** — project identity lives in committed `meridian.toml`; runtime and
  context state live under the user home, with resumable legacy migration.

## Resulting Behavior

Env composition happens once, the public env surface is deliberate, and project identity lives in the config file users already edit.

## Changes

The work was implemented as a coordinated migration because #361 and #336 share
the namespace boundary. Cross-repo consumers retain their registered public
contracts while Meridian's harness connections consume one bound environment.

## Work Item

issue-triage-sweep

## Verification

The shipped branch includes contract coverage for registry/source drift,
integration coverage at harness subprocess boundaries, project-identity migration
coverage, and stateless read-path coverage. The changes are recorded under
`[Unreleased]` in `CHANGELOG.md`.

## Knowledge Updates

Plan doc committed at `docs/plans/env-identity-ownership.md`. Triage evidence lives in the docs repo under `work/issue-triage-sweep/`.

## Spawn Trace

- p5268, p5269 (terra explorers) — issue triage lanes C/D
- two native sonnet reviewer lanes — issue triage lanes A/B
