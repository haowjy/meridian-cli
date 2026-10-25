---
name: escalation__meridian
description: When a delegated agent is refused access or needs a wider boundary.
spawn-only: true
---

# Privilege Escalation

Treat access as part of staffing. What you can do depends on your own sandbox:

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

For a blocked listener, URL, filesystem path, or tool call, return the concrete port,
destination, path, or permission error when the caller cannot authorize the narrowest
fix. For a context limit, choose a larger-context model from the live catalog rather
than widening permissions; model routing is not a way around the caller's boundary.

The one-run controls (`--sandbox`, `--network`, `--network-allow`, `--network-deny`,
`--approval`) are in `meridian spawn -h --advanced`. `--approval` governs tool-call
prompts, not the sandbox; surface fully automatic approval to the human first.
