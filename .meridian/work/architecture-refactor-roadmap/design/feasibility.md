# Feasibility — Architecture Refactor Roadmap

## Decision already made

Windows support is in scope for this roadmap as a **cross-cutting design constraint**.
It is not a separate "port the whole app" phase.

## What the constraint covers

Any stage that touches one of these boundaries must treat Windows/plain-directory semantics as part of the stage exit criteria:

- path resolution and root discovery
- process launch and child-process selection
- environment-variable projection
- shell invocation / argv shaping
- signals and termination behavior
- file locking
- sockets and control transport
- filesystem state, atomic writes, and runtime directories

## Feasibility implication

The repo already has the right shape for this work:

- `src/meridian/lib/platform/` holds the shared primitive layer
- `src/meridian/lib/state/user_paths.py` already centralizes user-home resolution
- launch process selection already has a narrow Windows-sensitive seam
- streaming already distinguishes transport shape, including TCP-vs-Unix socket behavior

The roadmap stays feasible if platform-specific behavior stays behind these seams instead of leaking into policy/domain code.

## Repeated platform checks are a smell

If the same `IS_WINDOWS` branch appears in multiple policy or domain modules, that is a cue to extract a narrow adapter or protocol boundary.

The target shape is:

- shared primitives in `meridian.lib.platform`
- focused platform/capability adapters at the boundary where semantics differ
- no broad `WindowsService` abstraction

That keeps cross-platform behavior local without forcing unrelated code to learn OS branching.
