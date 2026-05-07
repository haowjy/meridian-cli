# Preparatory Refactors — Architecture Refactor Roadmap

## Structural direction

Preserve `meridian.lib.platform` as the shared primitive layer.
Do **not** collapse Windows support into one giant service object.

Instead, add focused adapters only where semantics differ:

- launch/process selection
- control transport / streaming
- path and root discovery
- locking and file durability
- shell / argv shaping

## Boundary rule

Policy and domain modules should not accumulate platform branches.

If a module starts to repeat `IS_WINDOWS` checks, the design should pause and ask:

1. What boundary actually differs here?
2. Can the branch move into a narrow adapter or protocol?
3. Can callers depend on the abstraction instead of the OS check?

That keeps the branching near the mechanism seam rather than spreading it through orchestration code.

## Known primitives already in play

Examples that support the current direction:

- `src/meridian/lib/platform/`
- `src/meridian/lib/state/user_paths.py`
- launch process selector / `WindowsConsoleLauncher`
- streaming control transport split: TCP-vs-Unix socket

These are the right kind of seams: small, capability-specific, and reusable by higher-level policy.

## Stage-gate language

Every roadmap stage that touches platform-sensitive behavior must include an explicit gate:

- verify Windows behavior directly, or
- document the accepted limit if Windows/plain-directory semantics are intentionally out of scope for that stage

The gate should be named in the stage doc, not implied.

## Refactor policy

Prefer extraction over expansion.

When a stage needs platform-specific behavior, the default move is:

- extract a boundary adapter
- keep policy code OS-neutral
- localize the OS check inside the adapter

That is the migration path for this roadmap.
