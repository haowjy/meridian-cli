---
name: escalation
description: When a delegated agent is refused access or needs a wider boundary.
spawn-only: true
---

# Privilege Escalation

Treat access as part of staffing. What you can do depends on your own sandbox. The
one-run controls below are documented under `meridian spawn -h --advanced`.

- **You are sandboxed** (`read-only` or `workspace-write`): Meridian refuses a
  child with a weaker sandbox, broader network access, or no declared spawn
  capability. Retrying with broader access is refused; return the failure to your
  caller.
- **You are not sandboxed** (`danger-full-access`): a per-spawn `--sandbox` or
  `--network` override can work. Surface it to the human first and let them approve
  it; silently granting maximum access defeats the boundary.

## Escalation Discipline

Name the failed operation, exact error, and access needed so the next actor does not
repeat the diagnosis. Prefer a profile that already declares the access, then a
human-approved one-off override. Ask for a profile change only when the need recurs.

`--approval` controls tool-call prompts, not the sandbox: `default` leaves the choice
to the harness, `confirm` asks for each call, `auto` approves safe calls, and `never`
runs without approval prompts. `--yolo` is the shorthand for `--approval never`. Use the
least permissive mode that works and surface fully automatic approval to the human first.

## Model, Sandbox, and Network Controls

Use `-m MODEL_ALIAS` only when the task needs a model capability already allowed by
the profile. `meridian mars models list` shows aliases and routing;
`meridian mars models list --live` confirms the route is runnable. Model routing is
not a way around the caller's boundary.

`--sandbox` sets the one-run filesystem boundary. `--network allow|deny|allowlist`
sets shell network access. `--network-allow HOST` takes effect only with
`--network allowlist`; `--network-deny HOST` can narrow `allow`. A sandboxed caller
cannot grant a child more network or filesystem access than it has.

For a blocked listener, URL, filesystem path, or tool call, return the concrete port,
destination, path, or permission error when the caller cannot authorize the narrowest
fix. For a context limit, choose a larger-context model from the live catalog rather
than widening permissions.
