# Requirements: context active work resolution

## Problem
`meridian context` and `meridian context work` currently conflate configured context roots with active work-item state. This led agents to write directly under `.meridian/work/...` even when `meridian context` reported no active work item, and made the configured work root appear like magic/default local state instead of config-driven state.

## User intent
- `work` context root and active work item are separate concepts.
- The active work directory should be detected continuously, not assumed from stale launch-time state only.
- `MERIDIAN_ACTIVE_WORK_DIR` should be honored when set.
- If `MERIDIAN_ACTIVE_WORK_DIR` is unset or empty, `meridian context` should fall back to the current work item state, conceptually equivalent to `$(meridian work current)` if that resolves to something.
- Setting/switching active work should update the state that `meridian work current` reads, and spawned agents should receive the active work env var.

## Desired behavior sketch
- `MERIDIAN_CONTEXT_WORK_DIR`: configured work root override.
- `[context.work].path`: configured/default work root when env override absent.
- `MERIDIAN_ACTIVE_WORK_DIR`: active work-item override when set/non-empty.
- `meridian work current`: persisted/session current work item, if any.
- `meridian context`: displays configured work root and active work separately; active work resolves by env first, then work-current fallback.
- `meridian context work`: should be clarified/reworked so it does not hide active-work fallback semantics. Exact command shape needs design.

## Open design question
Should `meridian context work` return the configured work root only, or the active work dir when active exists? The user specifically suggested active work in `meridian context` should fall back to `$(meridian work current)`; command-level semantics still need alignment.
