---
name: escalation__meridian
description: When a delegated agent is refused access or needs a wider boundary.
mode: primary
---

# Privilege Escalation

Treat access as part of staffing. What you can do depends on your own sandbox:

- **You are sandboxed** (`read-only` or `workspace-write`): Meridian refuses a
  child with a weaker sandbox, broader network access, or no declared spawn
  capability. A child refused at your network floor can be relaunched with a narrower
  `--network` that fits inside yours; the refusal names both policies. Beyond that,
  retrying with broader access is refused; tell the human what failed and what access
  is needed.
- **You are not sandboxed** (`danger-full-access`): a per-spawn `--sandbox` or
  `--network` override can work. Surface it to the human first and let them approve
  it; silently granting maximum access defeats the boundary.

## Escalation Discipline

Name the failed operation, exact error, and access needed so the next actor does not
repeat the diagnosis. Prefer a profile that already declares the access, then a
human-approved one-off override. Ask for a profile change only when the need recurs.
When you cannot authorize the narrowest fix, tell the human the concrete port,
destination, path, or permission error it needs.

## Narrowest Fix by Symptom

The one-run controls are in `meridian spawn -h --advanced`.

- **Can't bind a port or start a server:** a `deny` network blocks local listeners.
  Try `--network allowlist --network-allow localhost` before `--network allow`.
- **Can't reach a URL:** use the profile's web tools when they fit; `--network` covers
  shell commands only. Otherwise allow just that host, within your own floor when
  sandboxed. `--network-allow` hosts are dropped unless the network mode is
  `allowlist`, so pass `--network allowlist` with them.
- **Tool call denied:** the approval mode is blocking it. On Claude, try
  `--approval auto` first: it approves file edits and keeps prompts for riskier calls.
  On OpenCode and Cursor, `auto` approves every call the profile's tools allow, the
  same as `--approval never` (`--yolo`), so ask the human before either there; ask
  before `never` on any harness. `--dry-run` reports what the mode becomes for the
  child's harness. Neither mode widens the sandbox or overrides a `tools` deny.
- **Context too small:** switch to a larger-context model with `-m` rather than
  widening permissions; `meridian mars models list --live` confirms a model runs on
  this machine. Model routing is not a way around the caller's boundary.
